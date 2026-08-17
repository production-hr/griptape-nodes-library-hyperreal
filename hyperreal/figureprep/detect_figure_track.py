from __future__ import annotations

import base64
import logging
import math
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

DOWNLOAD_TIMEOUT_SECONDS = 600
REGION_SCHEMA = "hyperreal.head_region/1"  # shared with the faceprep nodes; the shape is generic
DETECTOR_NAME = "chroma-figure"
KEY_COLORS = ["green", "blue"]
BOX_MODES = ["auto", "static", "tracked"]
STATIC_UNION_TOLERANCE = 1.15  # union area <= this x single-box area -> static
STAGE_MIN_GREEN_FRAC = 0.10  # a column is part of the stage when >=10% of it is backdrop
PREVIEW_CRF = 23


def _ffmpeg_paths() -> tuple[str, str]:
    """(ffmpeg, ffprobe) executables; static_ffmpeg downloads them on first use."""
    import static_ffmpeg.run

    return static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()


def _encoder_color_args(path: Path) -> list[str]:
    """Keep the source's colour matrix across a rawvideo round trip.

    Decoding to bgr24 uses the source's tagged matrix, but encoding raw frames
    back defaults to BT.601, which shifts colour on BT.709 source.
    """
    _, ffprobe = _ffmpeg_paths()
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=color_space,color_primaries,color_transfer",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    tags: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition("=")
        value = value.strip()
        if value and value not in ("unknown", "reserved", "N/A"):
            tags[key.strip()] = value
    if not tags:
        # Untagged source: ffmpeg uses the same default on decode and encode, so the
        # round trip is already symmetric. Forcing a matrix here would introduce the
        # very shift this function exists to prevent.
        return []
    return [
        "-colorspace", tags.get("color_space", "bt709"),
        "-color_primaries", tags.get("color_primaries", "bt709"),
        "-color_trc", tags.get("color_transfer", "bt709"),
    ]


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(values) <= 2:
        return values
    window = min(window, len(values))
    padded = np.pad(values.astype(np.float64), (window // 2, window - 1 - window // 2), mode="edge")
    kernel = np.ones(window) / window
    return np.convolve(padded, kernel, mode="valid")


class DetectFigureTrack(SuccessFailureNode):
    """Track a full figure on a chroma plate and emit an invertible crop region.

    Emits the same region dict as Detect Head Region, so Crop To Region and
    Reposition Tracked Crop both consume it. The window is full plate height and
    a single clip-wide width (widest silhouette + margin), panning horizontally
    on a smoothed track — a lazy camera operator, not a lock-on. Detection is
    per-column chroma coverage with the baseline (floor, spill) subtracted, so a
    non-green floor strip does not inflate the box.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/figureprep",
            "description": "Track a dancer/figure on a chroma plate and emit a full-height panning crop region.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The chroma-key plate video to analyze (figure against green/blue).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="key_color",
                type="str",
                default_value="green",
                tooltip="Backdrop colour. Subject = pixels that are NOT this colour.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=KEY_COLORS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="key_threshold",
                input_types=["int"],
                type="int",
                default_value=40,
                tooltip="How dominant the key channel must be (0-255) for a pixel to count as backdrop. "
                "Lower catches more spill as backdrop; raise if shadows on the screen read as subject.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="margin_px",
                input_types=["int"],
                type="int",
                default_value=48,
                tooltip="Horizontal breathing room added to each side of the widest silhouette.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="smoothing_window",
                input_types=["int"],
                type="int",
                default_value=25,
                tooltip="Moving-average window (frames) on the pan track. Bigger = lazier camera. "
                "The window is always clamped so the figure never exits it.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="max_pan_speed",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Pan speed limit in px/frame (0 = auto, ~1.5% of plate width). The track ramps in "
                "ahead of sudden moves (kicks, lunges) so this limit holds without losing the figure; "
                "it is only exceeded when the figure genuinely outruns it.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="box_mode",
                type="str",
                default_value="auto",
                tooltip="static = one fixed window; tracked = per-frame pan; auto decides from measured drift.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=BOX_MODES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="snap_multiple",
                type="int",
                default_value=16,
                tooltip="Advanced: crop width is snapped up to a multiple of this (video-codec friendly dims).",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="column_threshold",
                type="float",
                default_value=0.04,
                tooltip="Advanced: a column counts as subject when its baseline-subtracted coverage exceeds "
                "this fraction of the strongest column. Default 0.04 keeps thin extended arms "
                "(a limb is only a few percent of the figure's column height); raise it if spill "
                "blobs or shadows get grabbed as subject.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="region",
                output_type="json",
                tooltip="Region dict (same schema as Detect Head Region): source dims, box, mode, per-frame offsets.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        for flat_name in ("x", "y", "width", "height"):
            self.add_parameter(
                Parameter(
                    name=flat_name,
                    output_type="int",
                    tooltip=f"Flat box {flat_name} — wire straight into the stock Crop Video node.",
                    allowed_modes={ParameterMode.OUTPUT},
                )
            )
        self.add_parameter(
            Parameter(
                name="preview_video",
                output_type="VideoUrlArtifact",
                tooltip="The plate with the panning window drawn on it — eyeball this to catch a bad track.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the figure track result",
            result_details_placeholder="Track details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temp_path: Path | None = None
        try:
            source_path, temp_path = self._artifact_to_local_file(self.parameter_values.get("video"), "video")
            region, preview_path = self._track(source_path)

            self.parameter_output_values["region"] = region
            box = region["box"]
            for flat_name in ("x", "y", "width", "height"):
                self.parameter_output_values[flat_name] = box[flat_name]

            preview_name = f"figureprep_preview_{uuid.uuid4().hex[:8]}.mp4"
            try:
                saved_url = GriptapeNodes.StaticFilesManager().save_static_file(
                    preview_path.read_bytes(), preview_name
                )
                self.parameter_output_values["preview_video"] = VideoUrlArtifact(value=saved_url, name=preview_name)
            finally:
                preview_path.unlink(missing_ok=True)

            notes = region["notes"]
            warnings = []
            if notes["frames_missed"]:
                warnings.append(f"{notes['frames_missed']} frame(s) had no detectable figure; interpolated across gaps.")
            if notes["clamped"]:
                warnings.append("Crop width was clamped to the plate width — the figure (plus margin) spans the whole frame.")
            if notes["touches_edge_frames"]:
                warnings.append(
                    f"Figure touches the left/right frame edge on {notes['touches_edge_frames']} frame(s) — "
                    "the silhouette may be cut off in the plate itself."
                )
            warning_text = ("\nWARNING: " + "\nWARNING: ".join(warnings)) if warnings else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"{region['mode']} window {box['width']}x{box['height']} at ({box['x']}, {box['y']}), "
                    f"widest silhouette {notes['max_subject_width_px']}px, drift {notes['drift_px']}px over "
                    f"{region['source']['frame_count']} frames.{warning_text}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    # -- Tracking ------------------------------------------------------------

    def _subject_span(self, frame: np.ndarray, key_color: str, threshold: int, column_threshold: float) -> tuple[int, int] | None:
        """Per-column subject coverage within the screen area -> (x1, x2) span, or None.

        The screen itself bounds the search on all four sides, so nothing outside
        it — room walls, light stands, the wall above a sagging screen top, the
        floor in front, the cloth's own draped edges — can count as subject:

        1. Stage columns: horizontal extent of columns containing backdrop colour.
        2. Row band: robust percentiles of the screen's first/last green row, so
           only rows BETWEEN the screen's top and bottom edges are analyzed.
        3. Strict stage: within the band, columns must be substantially green,
           which drops the shadowed drape edges at the screen's left/right.
        4. Flagged runs touching the stage boundary are structure, not subject —
           a keyable figure is interior to the screen by definition.
        """
        b, g, r = frame[:, :, 0].astype(np.int16), frame[:, :, 1].astype(np.int16), frame[:, :, 2].astype(np.int16)
        if key_color == "blue":
            backdrop = (b - np.maximum(g, r)) > threshold
        else:
            backdrop = (g - np.maximum(b, r)) > threshold

        height = frame.shape[0]
        green_per_column = np.count_nonzero(backdrop, axis=0)
        stage_columns = np.flatnonzero(green_per_column >= height * STAGE_MIN_GREEN_FRAC)
        if stage_columns.size == 0:
            return None  # no screen in this frame at all
        s1, s2 = int(stage_columns[0]), int(stage_columns[-1]) + 1

        # Screen's own row extent (robust to per-column sag/pooling of a draped cloth).
        sub = backdrop[:, s1:s2]
        has_green = sub.any(axis=0)
        first_green = np.where(has_green, sub.argmax(axis=0), height)
        last_green = np.where(has_green, height - 1 - sub[::-1].argmax(axis=0), 0)
        band_top = int(np.percentile(first_green[has_green], 75))
        band_bottom = int(np.percentile(last_green[has_green], 25))
        if band_bottom <= band_top:
            band_top, band_bottom = 0, height - 1
        band = sub[band_top : band_bottom + 1]
        band_height = band.shape[0]

        # Strict stage: inside the band a screen column is mostly green; a drape
        # edge or room sliver is not. The figure's own columns fail this too, but
        # they are interior, so the first/last qualifying column still brackets them.
        coverage = np.count_nonzero(band, axis=0)
        strict = np.flatnonzero(coverage >= band_height * 0.5)
        if strict.size == 0:
            return None
        t1, t2 = int(strict[0]), int(strict[-1]) + 1  # stage-relative

        profile = np.count_nonzero(~band[:, t1:t2], axis=0).astype(np.float64)
        adjusted = profile - float(np.median(profile))
        peak = float(adjusted.max())
        min_peak = height * 0.02  # a real figure fills at least 2% of some column
        if peak < min_peak:
            return None
        flagged = adjusted >= max(column_threshold * peak, min_peak * 0.5)
        columns = np.flatnonzero(flagged)
        if columns.size == 0:
            return None
        # The figure is ONE contiguous run of columns (limbs attach to the body), so
        # keep only the run with the greatest total mass. Disconnected blobs are
        # discarded wherever they sit, and mass — not tallest-single-column — is the
        # discriminator because a thin full-height obstruction (a light-stand pole in
        # front of the screen) can out-peak a motion-blurred dancer, but a ~10-column
        # pole never out-masses a ~300-column figure. Gaps up to a few columns are
        # bridged (motion blur can thin a wrist).
        max_gap = 8
        breaks = np.flatnonzero(np.diff(columns) > max_gap)
        run_starts = np.concatenate(([0], breaks + 1))
        run_ends = np.concatenate((breaks, [columns.size - 1]))
        best_span, best_mass = None, 0.0
        for start, end in zip(run_starts, run_ends, strict=True):
            lo_col, hi_col = int(columns[start]), int(columns[end])
            mass = float(adjusted[lo_col : hi_col + 1].sum())
            if mass > best_mass:
                best_mass = mass
                best_span = (s1 + t1 + lo_col, s1 + t1 + hi_col + 1)
        return best_span

    def _track(self, source_path: Path) -> tuple[dict, Path]:
        key_color = self.parameter_values.get("key_color") or "green"
        threshold = int(self.parameter_values.get("key_threshold") or 40)
        margin = max(0, int(self.parameter_values.get("margin_px") or 48))
        column_threshold = float(self.parameter_values.get("column_threshold") or 0.04)

        cap = cv2.VideoCapture(str(source_path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video {source_path.name} with OpenCV.")
        try:
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
            metadata_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            spans: dict[int, tuple[int, int]] = {}
            touches_edge_frames = 0
            frame_index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                span = self._subject_span(frame, key_color, threshold, column_threshold)
                if span is not None:
                    spans[frame_index] = span
                    if span[0] <= 0 or span[1] >= width:
                        touches_edge_frames += 1
                frame_index += 1
            frame_count = frame_index
        finally:
            cap.release()

        ffprobe_count = self._ffprobe_frame_count(source_path)
        if not spans:
            raise RuntimeError(
                f"No figure found in any of {frame_count} frames — nothing stood out against the "
                f"{key_color} backdrop (threshold {threshold}). Check key_color, or lower key_threshold."
            )

        # One clip-wide window: full plate height, widest silhouette + margin, snapped up.
        max_subject_width = max(x2 - x1 for (x1, x2) in spans.values())
        snap = max(1, int(self.parameter_values.get("snap_multiple") or 16))
        crop_w = int(math.ceil((max_subject_width + 2 * margin) / snap) * snap)
        clamped = False
        if crop_w > width:
            crop_w = (width // snap) * snap if width >= snap else width
            clamped = True
        crop_h = height - (height % 2)

        offsets, drift_px = self._build_offsets(spans, crop_w, frame_count, width)

        min_x, max_x = int(offsets.min()), int(offsets.max())
        union_area = (max_x - min_x + crop_w) * crop_h
        mode = self.parameter_values.get("box_mode") or "auto"
        if mode == "auto":
            mode = "static" if union_area <= STATIC_UNION_TOLERANCE * crop_w * crop_h else "tracked"
        union_origin_x = min(max(min_x, 0), width - crop_w)
        if mode == "static":
            offsets = np.full(frame_count, union_origin_x, dtype=np.int64)

        region = {
            "schema": REGION_SCHEMA,
            "source": {"width": width, "height": height, "frame_rate": fps, "frame_count": frame_count},
            "box": {
                "x": union_origin_x,
                "y": 0,
                "width": crop_w,
                "height": crop_h,
                "confidence": 1.0,
            },
            "mode": mode,
            "offsets": [[int(x), 0] for x in offsets],
            "detector": DETECTOR_NAME,
            "notes": {
                "drift_px": drift_px,
                "frames_missed": frame_count - len(spans),
                "clamped": clamped,
                "touches_edge_frames": touches_edge_frames,
                "max_subject_width_px": int(max_subject_width),
                "metadata_frame_count": metadata_count,
                "ffprobe_frame_count": ffprobe_count,
            },
        }
        preview_path = self._render_preview(source_path, region)
        return region, preview_path

    def _build_offsets(
        self, spans: dict[int, tuple[int, int]], crop_w: int, frame_count: int, width: int
    ) -> tuple[np.ndarray, int]:
        """Per-frame window x offsets: smoothed desired track, then a speed-limited path
        through the per-frame containment intervals.

        Containment is a hard constraint (the silhouette never exits the window) and pan
        speed a soft one. Feasible intervals are propagated backward in time first, so the
        track ramps in AHEAD of a sudden kick instead of jerking when it lands; the speed
        limit is only exceeded when the figure genuinely outruns it (feasible set empty).
        """
        indices = np.array(sorted(spans.keys()), dtype=np.float64)
        centers = np.array([(spans[int(i)][0] + spans[int(i)][1]) / 2.0 for i in indices])
        frames = np.arange(frame_count, dtype=np.float64)
        desired = np.interp(frames, indices, centers) - crop_w / 2.0
        window = max(1, int(self.parameter_values.get("smoothing_window") or 25))
        desired = _moving_average(desired, window)

        # Allowed interval per frame: the whole silhouette inside the window, inside the plate.
        max_x_allowed = max(0, width - crop_w)
        lo = np.zeros(frame_count)
        hi = np.full(frame_count, float(max_x_allowed))
        for idx, (x1, x2) in spans.items():
            lo[idx] = max(0.0, float(x2 - crop_w))
            hi[idx] = min(float(max_x_allowed), float(x1))
        bad = lo > hi  # silhouette wider than the window (only when clamped): center it instead
        mid = (lo + hi) / 2.0
        lo[bad] = mid[bad]
        hi[bad] = mid[bad]

        # Median-3 the constraint tracks: a single-frame silhouette-edge spike (motion
        # blur) must not yank the window for one frame and release it — that reads as
        # jitter. Real moves last 2+ frames and survive the filter; the margin absorbs
        # the one-frame excursions this smooths over.
        if frame_count >= 3:
            def _median3(values: np.ndarray) -> np.ndarray:
                padded = np.pad(values, 1, mode="edge")
                return np.median(np.stack([padded[:-2], padded[1:-1], padded[2:]]), axis=0)

            lo = _median3(lo)
            hi = _median3(hi)
            crossed = lo > hi  # independent medians can cross; re-center those frames
            mid = (lo + hi) / 2.0
            lo[crossed] = mid[crossed]
            hi[crossed] = mid[crossed]

        max_step = float(self.parameter_values.get("max_pan_speed") or 0)
        if max_step <= 0:
            max_step = max(8.0, width * 0.015)

        # Backward reachability: shrink each frame's interval to positions from which the
        # rest of the clip is still satisfiable at the speed limit. An empty intersection
        # means the figure outruns the limit there — keep the raw interval (containment wins).
        flo, fhi = lo.copy(), hi.copy()
        for i in range(frame_count - 2, -1, -1):
            reach_lo, reach_hi = flo[i + 1] - max_step, fhi[i + 1] + max_step
            new_lo, new_hi = max(flo[i], reach_lo), min(fhi[i], reach_hi)
            if new_lo <= new_hi:
                flo[i], fhi[i] = new_lo, new_hi

        # Greedy forward walk: follow the desired track within the feasible tube.
        x = np.empty(frame_count)
        x[0] = np.clip(desired[0], flo[0], fhi[0])
        for i in range(1, frame_count):
            step_lo = max(flo[i], x[i - 1] - max_step)
            step_hi = min(fhi[i], x[i - 1] + max_step)
            if step_lo > step_hi:  # speed limit infeasible here: jump to containment
                step_lo, step_hi = flo[i], fhi[i]
            x[i] = np.clip(desired[i], step_lo, step_hi)

        x = np.clip(np.rint(x), 0, max_x_allowed).astype(np.int64)
        drift_px = int(x.max() - x.min()) if frame_count else 0
        return x, drift_px

    def _render_preview(self, source_path: Path, region: dict) -> Path:
        """Draw the panning window on every frame; encode via an ffmpeg rawvideo pipe."""
        ffmpeg, _ = _ffmpeg_paths()
        source = region["source"]
        box_w, box_h = region["box"]["width"], region["box"]["height"]
        offsets = region["offsets"]
        out_path = Path(tempfile.gettempdir()) / f"figureprep_preview_{uuid.uuid4().hex}.mp4"

        encoder = subprocess.Popen(
            [
                ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{source['width']}x{source['height']}", "-r", f"{source['frame_rate']:.6f}",
                "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", str(PREVIEW_CRF), "-pix_fmt", "yuv420p",
                *_encoder_color_args(source_path), str(out_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cap = cv2.VideoCapture(str(source_path))
        try:
            thickness = max(2, box_w // 128)
            index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                x, y = offsets[min(index, len(offsets) - 1)]
                cv2.rectangle(frame, (x, y), (x + box_w, y + box_h), (255, 0, 255), thickness)
                encoder.stdin.write(frame.tobytes())
                index += 1
        finally:
            cap.release()
            encoder.stdin.close()
            encoder.wait()
        if encoder.returncode != 0 or not out_path.is_file():
            raise RuntimeError("ffmpeg failed while encoding the preview video.")
        return out_path

    def _ffprobe_frame_count(self, source_path: Path) -> int:
        try:
            _, ffprobe = _ffmpeg_paths()
            result = subprocess.run(
                [
                    ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                    "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(source_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            return int(result.stdout.strip() or 0)
        except Exception:
            logger.warning("ffprobe frame count failed for %s", source_path, exc_info=True)
            return 0

    # -- Media input handling (copied from hyperreal/heygen/avatar_video.py;
    # node files are self-contained by library convention) ------------------

    def _artifact_to_local_file(self, artifact: Any, label: str) -> tuple[Path, Path | None]:
        """Resolve an artifact to a local file path; second element is a temp file to delete, if any."""
        value = getattr(artifact, "value", artifact)
        if isinstance(value, str) and value and not value.startswith(("http://", "https://", "data:")):
            path = self._resolve_workspace_path(value)
            if path is not None:
                return path, None
        data = self._artifact_to_bytes(artifact, label)
        handle = Path(tempfile.gettempdir()) / f"figureprep_{label}_{uuid.uuid4().hex}.mp4"
        handle.write_bytes(data)
        return handle, handle

    def _artifact_to_bytes(self, artifact: Any, label: str) -> bytes:
        if artifact is None:
            raise ValueError(f"No {label} input connected.")
        value = getattr(artifact, "value", artifact)
        if isinstance(value, bytes):
            return value
        if isinstance(value, str) and value:
            if value.startswith("data:"):
                _, _, encoded = value.partition(",")
                return base64.b64decode(encoded)
            if value.startswith(("http://", "https://")):
                response = requests.get(value, timeout=DOWNLOAD_TIMEOUT_SECONDS)
                if not response.ok:
                    raise RuntimeError(f"Could not download {label} from {value} (HTTP {response.status_code}).")
                return response.content
            path = self._resolve_workspace_path(value)
            if path is not None:
                return path.read_bytes()
        preview = repr(value)[:120]
        raise ValueError(f"Unsupported {label} input of type {type(artifact).__name__} (value: {preview}).")

    def _resolve_workspace_path(self, value: str) -> Path | None:
        """Resolve non-URL artifact values: '{project_dir}/...' macros, workspace-relative, or absolute paths."""
        if "{" in value:
            return self._resolve_macro_path(value)
        path = Path(value)
        if path.is_absolute():
            return path if path.is_file() else None
        try:
            workspace = GriptapeNodes.ConfigManager().workspace_path
        except Exception:
            return None
        candidate = Path(workspace) / path
        return candidate if candidate.is_file() else None

    def _resolve_macro_path(self, value: str) -> Path | None:
        # Lazy import: the project/macro system only exists on newer engines,
        # and the library must still load without it.
        try:
            from griptape_nodes.common.macro_parser import ParsedMacro
            from griptape_nodes.retained_mode.events.project_events import (
                GetPathForMacroRequest,
                GetPathForMacroResultSuccess,
            )

            result = GriptapeNodes.handle_request(GetPathForMacroRequest(parsed_macro=ParsedMacro(value), variables={}))
        except Exception:
            logger.warning("Could not resolve macro path %r", value, exc_info=True)
            return None
        if isinstance(result, GetPathForMacroResultSuccess):
            path = Path(result.absolute_path)
            if path.is_file():
                return path
        logger.warning("Macro path %r did not resolve to an existing file", value)
        return None
