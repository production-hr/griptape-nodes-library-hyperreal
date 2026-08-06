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
REGION_SCHEMA = "hyperreal.head_region/1"
DETECTOR_NAME = "yunet-2023mar"
MODEL_PATH = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
BOX_MODES = ["auto", "static", "tracked"]
STATIC_UNION_TOLERANCE = 1.15  # union area <= this x single-box area -> static
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


class DetectHeadRegion(SuccessFailureNode):
    """Find the head in a talking-head video and emit an invertible crop region.

    Emits the hyperreal.head_region/1 dict consumed by Crop To Region and
    Composite Region Back. Box size is constant for the whole clip (only the
    position moves), so crop and paste-back are pure pixel copies.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/faceprep",
            "description": "Detect the head in a video and emit a square crop region for face-swap prep.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The plate video to analyze.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="head_scale",
                input_types=["float"],
                type="float",
                default_value=1.6,
                tooltip="Face box -> head box expansion factor. Only used when all pad_* values are 0; "
                "the default pads normally take precedence.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="subject_index",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="0 = highest-confidence face. For multi-person plates, 1 = second-ranked face, etc. "
                "After the first hit the subject is followed by nearest centroid, not re-ranked.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="box_mode",
                type="str",
                default_value="auto",
                tooltip="static = one fixed box; tracked = per-frame offsets; auto decides from measured drift.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=BOX_MODES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="snap_multiple",
                type="int",
                default_value=64,
                tooltip="Final box dimensions are snapped up to a multiple of this.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="detection_interval",
                input_types=["int"],
                type="int",
                default_value=5,
                tooltip="Run the detector every Nth frame and interpolate between samples.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="confidence_threshold",
                input_types=["float"],
                type="float",
                default_value=0.6,
                tooltip="Minimum detector confidence for a face to count.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="pad_top",
                type="float",
                default_value=0.9,
                tooltip="Advanced: padding above the face as a fraction of face height (most, for hair). "
                "Set all pads to 0 to use head_scale instead.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="pad_bottom",
                type="float",
                default_value=0.5,
                tooltip="Advanced: padding below the face as a fraction of face height.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="pad_sides",
                type="float",
                default_value=0.35,
                tooltip="Advanced: padding each side of the face as a fraction of face width.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="smoothing_window",
                type="int",
                default_value=9,
                tooltip="Advanced: moving-average window (frames) over offsets before rounding to int.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="region",
                output_type="json",
                tooltip="hyperreal.head_region/1 dict: source dims, box, mode, per-frame offsets.",
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
                tooltip="The plate with the detected box drawn on it — eyeball this to catch a bad detection.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the head detection result",
            result_details_placeholder="Detection details will appear here.",
        )

    def validate_before_node_run(self) -> list[Exception] | None:
        if not MODEL_PATH.is_file():
            return [FileNotFoundError(f"Vendored detector weights missing: {MODEL_PATH}")]
        return None

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temp_path: Path | None = None
        try:
            source_path, temp_path = self._artifact_to_local_file(self.parameter_values.get("video"), "video")
            region, preview_path = self._detect(source_path)

            self.parameter_output_values["region"] = region
            box = region["box"]
            for flat_name in ("x", "y", "width", "height"):
                self.parameter_output_values[flat_name] = box[flat_name]

            preview_name = f"faceprep_preview_{uuid.uuid4().hex[:8]}.mp4"
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
                warnings.append(f"{notes['frames_missed']} sampled frame(s) had no face; interpolated across gaps.")
            if notes["clamped"]:
                warnings.append("Box was clamped to source dimensions — the head leaves frame at some point.")
            warning_text = ("\nWARNING: " + "\nWARNING: ".join(warnings)) if warnings else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"{region['mode']} box {box['width']}x{box['height']} at ({box['x']}, {box['y']}), "
                    f"confidence {box['confidence']:.2f}, drift {notes['drift_px']}px over "
                    f"{region['source']['frame_count']} frames.{warning_text}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    # -- Detection ----------------------------------------------------------

    def _detect(self, source_path: Path) -> tuple[dict, Path]:
        interval = max(1, int(self.parameter_values.get("detection_interval") or 5))
        confidence_threshold = float(self.parameter_values.get("confidence_threshold") or 0.6)
        subject_index = max(0, int(self.parameter_values.get("subject_index") or 0))

        cap = cv2.VideoCapture(str(source_path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video {source_path.name} with OpenCV.")
        try:
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
            metadata_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (width, height), confidence_threshold)
            detector.setInputSize((width, height))

            samples: dict[int, tuple[float, float, float, float, float]] = {}  # idx -> face x,y,w,h,conf
            missed_sample_indices: list[int] = []
            previous_center: tuple[float, float] | None = None
            frame_index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if frame_index % interval == 0:
                    face = self._pick_face(detector.detect(frame)[1], previous_center, subject_index)
                    if face is None:
                        missed_sample_indices.append(frame_index)
                    else:
                        fx, fy, fw, fh, conf = face
                        samples[frame_index] = (fx, fy, fw, fh, conf)
                        previous_center = (fx + fw / 2.0, fy + fh / 2.0)
                frame_index += 1
            frame_count = frame_index
        finally:
            cap.release()

        # Actual decoded count is authoritative; note when the metadata disagreed
        # (cv2's CAP_PROP_FRAME_COUNT is unreliable on VFR sources).
        ffprobe_count = self._ffprobe_frame_count(source_path)
        if not samples:
            sampled = math.ceil(frame_count / interval) if frame_count else 0
            raise RuntimeError(
                f"No faces found in any of {sampled} sampled frames "
                f"(threshold {confidence_threshold}). Lower confidence_threshold or check the plate."
            )

        centers, side = self._head_boxes(samples, width, height)
        snap = max(1, int(self.parameter_values.get("snap_multiple") or 64))
        side = int(math.ceil(side / snap) * snap)
        clamped = False
        max_side = min(width, height)
        if side > max_side:
            side = (max_side // snap) * snap if max_side >= snap else max_side
            clamped = True

        offsets = self._build_offsets(centers, side, frame_count, width, height)
        min_x, max_x = int(offsets[:, 0].min()), int(offsets[:, 0].max())
        min_y, max_y = int(offsets[:, 1].min()), int(offsets[:, 1].max())
        drift_px = max(max_x - min_x, max_y - min_y)

        union_area = (max_x - min_x + side) * (max_y - min_y + side)
        mode = self.parameter_values.get("box_mode") or "auto"
        if mode == "auto":
            mode = "static" if union_area <= STATIC_UNION_TOLERANCE * side * side else "tracked"
        union_origin = (
            min(max(min_x, 0), width - side),
            min(max(min_y, 0), height - side),
        )
        if mode == "static":
            offsets = np.tile(np.array(union_origin, dtype=np.int64), (frame_count, 1))

        best_confidence = max(conf for (_, _, _, _, conf) in samples.values())
        region = {
            "schema": REGION_SCHEMA,
            "source": {"width": width, "height": height, "frame_rate": fps, "frame_count": frame_count},
            "box": {
                "x": union_origin[0],
                "y": union_origin[1],
                "width": side,
                "height": side,
                "confidence": round(best_confidence, 4),
            },
            "mode": mode,
            "offsets": [[int(x), int(y)] for x, y in offsets],
            "detector": DETECTOR_NAME,
            "notes": {
                "drift_px": drift_px,
                "detection_interval": interval,
                "frames_missed": len(missed_sample_indices),
                "clamped": clamped,
                "metadata_frame_count": metadata_count,
                "ffprobe_frame_count": ffprobe_count,
            },
        }
        preview_path = self._render_preview(source_path, region)
        return region, preview_path

    def _pick_face(
        self, faces: np.ndarray | None, previous_center: tuple[float, float] | None, subject_index: int
    ) -> tuple[float, float, float, float, float] | None:
        """Rank by confidence on the first hit, then follow by nearest centroid."""
        if faces is None or len(faces) == 0:
            return None
        candidates = [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])) for f in faces]
        if previous_center is None:
            candidates.sort(key=lambda c: c[4], reverse=True)
            if subject_index >= len(candidates):
                return None
            return candidates[subject_index]
        px, py = previous_center
        return min(candidates, key=lambda c: (c[0] + c[2] / 2 - px) ** 2 + (c[1] + c[3] / 2 - py) ** 2)

    def _head_boxes(
        self, samples: dict[int, tuple[float, float, float, float, float]], width: int, height: int
    ) -> tuple[dict[int, tuple[float, float]], float]:
        """Face boxes -> head-box centers plus the single clip-wide square side."""
        pad_top = float(self.parameter_values.get("pad_top") or 0.0)
        pad_bottom = float(self.parameter_values.get("pad_bottom") or 0.0)
        pad_sides = float(self.parameter_values.get("pad_sides") or 0.0)
        use_pads = (pad_top + pad_bottom + pad_sides) > 0
        head_scale = float(self.parameter_values.get("head_scale") or 1.6)

        centers: dict[int, tuple[float, float]] = {}
        max_side = 0.0
        for idx, (fx, fy, fw, fh, _) in samples.items():
            if use_pads:
                x0 = fx - pad_sides * fw
                x1 = fx + fw + pad_sides * fw
                y0 = fy - pad_top * fh
                y1 = fy + fh + pad_bottom * fh
            else:
                cx, cy = fx + fw / 2, fy + fh / 2
                x0, x1 = cx - fw * head_scale / 2, cx + fw * head_scale / 2
                y0, y1 = cy - fh * head_scale / 2, cy + fh * head_scale / 2
            side = max(x1 - x0, y1 - y0)
            centers[idx] = ((x0 + x1) / 2, (y0 + y1) / 2)
            max_side = max(max_side, side)
        return centers, max_side

    def _build_offsets(
        self, centers: dict[int, tuple[float, float]], side: int, frame_count: int, width: int, height: int
    ) -> np.ndarray:
        """Sampled centers -> per-frame integer top-left offsets, interpolated, smoothed, clamped."""
        indices = np.array(sorted(centers.keys()), dtype=np.float64)
        cx = np.array([centers[int(i)][0] for i in indices])
        cy = np.array([centers[int(i)][1] for i in indices])
        frames = np.arange(frame_count, dtype=np.float64)
        ox = np.interp(frames, indices, cx) - side / 2
        oy = np.interp(frames, indices, cy) - side / 2

        window = max(1, int(self.parameter_values.get("smoothing_window") or 9))
        ox = _moving_average(ox, window)
        oy = _moving_average(oy, window)

        ox = np.clip(np.rint(ox), 0, max(0, width - side)).astype(np.int64)
        oy = np.clip(np.rint(oy), 0, max(0, height - side)).astype(np.int64)
        return np.stack([ox, oy], axis=1)

    def _render_preview(self, source_path: Path, region: dict) -> Path:
        """Draw the box on every frame; encode via an ffmpeg rawvideo pipe."""
        ffmpeg, _ = _ffmpeg_paths()
        source = region["source"]
        side = region["box"]["width"]
        offsets = region["offsets"]
        out_path = Path(tempfile.gettempdir()) / f"faceprep_preview_{uuid.uuid4().hex}.mp4"

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
            thickness = max(2, side // 128)
            index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                x, y = offsets[min(index, len(offsets) - 1)]
                cv2.rectangle(frame, (x, y), (x + side, y + side), (0, 255, 0), thickness)
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
        handle = Path(tempfile.gettempdir()) / f"faceprep_{label}_{uuid.uuid4().hex}.mp4"
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
