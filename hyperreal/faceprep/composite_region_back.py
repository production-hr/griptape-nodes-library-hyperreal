from __future__ import annotations

import base64
import json
import logging
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
EDGE_SHAPES = ["ellipse", "rounded_rect"]
AUDIO_SOURCES = ["plate", "none"]
FPS_TOLERANCE = 0.01


def _ffmpeg_paths() -> tuple[str, str]:
    """(ffmpeg, ffprobe) executables; static_ffmpeg downloads them on first use."""
    import static_ffmpeg.run

    return static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()


def _encoder_color_args(path: Path) -> list[str]:
    """Keep the source's colour matrix across a rawvideo round trip.

    Decoding to bgr24 uses the source's tagged matrix, but encoding raw frames
    back defaults to BT.601. On BT.709 source that shifts every pixel, touched or
    not — measured on a saturated plate, green 160 -> 142 and red clipped to 0.
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


def _parse_region(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or value.get("schema") != REGION_SCHEMA:
        raise ValueError(f"region input is not a {REGION_SCHEMA} dict (connect Detect or Crop's region output).")
    return value


class CompositeRegionBack(SuccessFailureNode):
    """Paste a (swapped, upscaled) head clip back onto the original plate.

    Validates frame count / fps / dimensions before doing any work — a
    mismatched insert desyncs progressively and looks like a tracking bug,
    so it fails loudly instead.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/faceprep",
            "description": "Composite the swapped head region back onto the original plate video.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="plate_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The original plate video.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="insert_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The swapped (and upscaled) head clip to paste back.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="region",
                input_types=["json"],
                type="json",
                tooltip="hyperreal.head_region/1 dict from Detect Head Region or Crop To Region.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="mask_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="Optional white-on-black matte at insert resolution (e.g. from a SAM video node). "
                "Overrides edge_shape.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="feather_px",
                input_types=["int"],
                type="int",
                default_value=24,
                tooltip="Feather width in pixels at box scale.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="mask_shrink_px",
                input_types=["int"],
                type="int",
                default_value=8,
                tooltip="Erode the mask by this many pixels before feathering, so the blend edge sits inside "
                "the detected region.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="edge_shape",
                type="str",
                default_value="ellipse",
                tooltip="Procedural matte shape. Ignored when mask_video is connected.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=EDGE_SHAPES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="color_match",
                type="bool",
                default_value=False,
                tooltip="Mean/std match of the insert to the plate region in LAB, per frame.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="audio_source",
                type="str",
                default_value="plate",
                tooltip="Where the output audio comes from.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=AUDIO_SOURCES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="crf",
                input_types=["int"],
                type="int",
                default_value=16,
                tooltip="x264 CRF for the composited output.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the composited video into, e.g. {project_dir}/outputs. "
                "Leave empty to skip saving a file copy.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="composited_video",
                output_type="VideoUrlArtifact",
                tooltip="The plate with the insert composited back.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the composite result",
            result_details_placeholder="Composite details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temp_files: list[Path] = []
        try:
            region = _parse_region(self.parameter_values.get("region"))
            plate_path, tmp = self._artifact_to_local_file(self.parameter_values.get("plate_video"), "plate_video")
            if tmp:
                temp_files.append(tmp)
            insert_path, tmp = self._artifact_to_local_file(self.parameter_values.get("insert_video"), "insert_video")
            if tmp:
                temp_files.append(tmp)
            mask_path: Path | None = None
            if self.parameter_values.get("mask_video") is not None:
                mask_path, tmp = self._artifact_to_local_file(self.parameter_values.get("mask_video"), "mask_video")
                if tmp:
                    temp_files.append(tmp)

            self._validate_inputs(region, plate_path, insert_path)

            video_tmp = Path(tempfile.gettempdir()) / f"faceprep_composite_{uuid.uuid4().hex}.mp4"
            temp_files.append(video_tmp)
            frames_inserted = self._composite(region, plate_path, insert_path, mask_path, video_tmp)

            out_tmp = self._attach_audio(video_tmp, plate_path)
            if out_tmp != video_tmp:
                temp_files.append(out_tmp)

            data = out_tmp.read_bytes()
            filename = f"faceprep_composited_{uuid.uuid4().hex[:8]}.mp4"
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
            self._saved_files: list[str] = []
            self._save_copy_to_output_directory(data, filename)
            self.parameter_output_values["composited_video"] = VideoUrlArtifact(value=saved_url, name=filename)

            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Composited {frames_inserted} frames back onto the plate "
                    f"({len(data) / (1024 * 1024):.1f} MB).{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            for path in temp_files:
                path.unlink(missing_ok=True)

    # -- Validation ---------------------------------------------------------

    def _probe(self, path: Path) -> tuple[int, int, float, int]:
        """(width, height, fps, frame_count) — count via ffprobe packets, rest via cv2."""
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video {path.name} with OpenCV.")
        try:
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
        finally:
            cap.release()
        _, ffprobe = _ffmpeg_paths()
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        frame_count = int(result.stdout.strip() or 0)
        return width, height, fps, frame_count

    def _validate_inputs(self, region: dict, plate_path: Path, insert_path: Path) -> None:
        source = region["source"]
        plate_w, plate_h, plate_fps, _ = self._probe(plate_path)
        insert_w, insert_h, insert_fps, insert_count = self._probe(insert_path)

        if (plate_w, plate_h) != (source["width"], source["height"]):
            raise ValueError(
                f"Plate is {plate_w}x{plate_h} but the region was detected on "
                f"{source['width']}x{source['height']} — wrong plate for this region."
            )
        if insert_count != source["frame_count"]:
            raise ValueError(
                f"Insert has {insert_count} frames but the region covers {source['frame_count']} — "
                "the swap or upscale changed the frame count (did Topaz frame interpolation get enabled?). "
                "Composite would desync progressively, so this fails instead."
            )
        if abs(insert_fps - plate_fps) > FPS_TOLERANCE:
            raise ValueError(f"Insert fps ({insert_fps:.3f}) does not match plate fps ({plate_fps:.3f}).")
        if insert_w != insert_h:
            logger.warning("Insert is %dx%d (not square); it will be resized to the region box.", insert_w, insert_h)

    # -- Compositing --------------------------------------------------------

    def _procedural_alpha(self, side: int) -> np.ndarray:
        shrink = max(0, int(self.parameter_values.get("mask_shrink_px") or 0))
        feather = max(0, int(self.parameter_values.get("feather_px") or 0))
        shape = self.parameter_values.get("edge_shape") or "ellipse"
        mask = np.zeros((side, side), dtype=np.uint8)
        inset = shrink + max(1, feather // 2)
        if shape == "ellipse":
            center = (side // 2, side // 2)
            axes = (max(1, side // 2 - inset), max(1, side // 2 - inset))
            cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
        else:
            radius = max(2, side // 8)
            x0 = y0 = inset
            x1 = y1 = side - 1 - inset
            cv2.rectangle(mask, (x0 + radius, y0), (x1 - radius, y1), 255, -1)
            cv2.rectangle(mask, (x0, y0 + radius), (x1, y1 - radius), 255, -1)
            for cx, cy in ((x0 + radius, y0 + radius), (x1 - radius, y0 + radius),
                           (x0 + radius, y1 - radius), (x1 - radius, y1 - radius)):
                cv2.circle(mask, (cx, cy), radius, 255, -1)
        return self._finish_alpha(mask, feather, erode_px=0)

    def _finish_alpha(self, mask: np.ndarray, feather: int, erode_px: int) -> np.ndarray:
        if erode_px > 0:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode_px * 2 + 1, erode_px * 2 + 1))
            mask = cv2.erode(mask, kernel)
        if feather > 0:
            mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(0.5, feather / 3.0))
        return (mask.astype(np.float32) / 255.0)[:, :, np.newaxis]

    @staticmethod
    def _match_color(insert: np.ndarray, plate_region: np.ndarray) -> np.ndarray:
        lab_insert = cv2.cvtColor(insert, cv2.COLOR_BGR2LAB).astype(np.float32)
        lab_plate = cv2.cvtColor(plate_region, cv2.COLOR_BGR2LAB).astype(np.float32)
        for channel in range(3):
            mean_i, std_i = lab_insert[..., channel].mean(), lab_insert[..., channel].std()
            mean_p, std_p = lab_plate[..., channel].mean(), lab_plate[..., channel].std()
            lab_insert[..., channel] = (lab_insert[..., channel] - mean_i) / max(std_i, 1e-6) * std_p + mean_p
        return cv2.cvtColor(np.clip(lab_insert, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)

    def _composite(
        self, region: dict, plate_path: Path, insert_path: Path, mask_path: Path | None, out_path: Path
    ) -> int:
        ffmpeg, _ = _ffmpeg_paths()
        source = region["source"]
        side = region["box"]["width"]
        offsets = region["offsets"]
        plate_w, plate_h = source["width"], source["height"]
        plate_frame_bytes = plate_w * plate_h * 3
        insert_w, insert_h, _, _ = self._probe(insert_path)
        insert_frame_bytes = insert_w * insert_h * 3
        color_match = bool(self.parameter_values.get("color_match", False))
        feather = max(0, int(self.parameter_values.get("feather_px") or 0))
        shrink = max(0, int(self.parameter_values.get("mask_shrink_px") or 0))

        def raw_decoder(path: Path) -> subprocess.Popen:
            return subprocess.Popen(
                [ffmpeg, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

        plate_dec = raw_decoder(plate_path)
        insert_dec = raw_decoder(insert_path)
        mask_dec = raw_decoder(mask_path) if mask_path is not None else None
        encoder = subprocess.Popen(
            [
                ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{plate_w}x{plate_h}", "-r", f"{source['frame_rate']:.6f}",
                "-i", "-", "-an", "-c:v", "libx264", "-preset", "slow",
                "-crf", str(int(self.parameter_values.get("crf") or 16)), "-pix_fmt", "yuv420p",
                *_encoder_color_args(plate_path), str(out_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        static_alpha = None if mask_path is not None else self._procedural_alpha(side)
        frames_inserted = 0
        frame_index = 0
        try:
            while True:
                plate_chunk = plate_dec.stdout.read(plate_frame_bytes)
                if len(plate_chunk) < plate_frame_bytes:
                    break
                frame = np.frombuffer(plate_chunk, dtype=np.uint8).reshape(plate_h, plate_w, 3).copy()

                insert_chunk = insert_dec.stdout.read(insert_frame_bytes)
                if len(insert_chunk) == insert_frame_bytes:
                    insert = np.frombuffer(insert_chunk, dtype=np.uint8).reshape(insert_h, insert_w, 3)
                    if (insert_w, insert_h) != (side, side):
                        # Downscale from upscaled resolution — the safe resampling direction.
                        insert = cv2.resize(insert, (side, side), interpolation=cv2.INTER_LANCZOS4)
                    x, y = offsets[min(frame_index, len(offsets) - 1)]
                    plate_region = frame[y : y + side, x : x + side]
                    if color_match:
                        insert = self._match_color(insert, plate_region)
                    if mask_dec is not None:
                        mask_chunk = mask_dec.stdout.read(insert_frame_bytes)
                        if len(mask_chunk) == insert_frame_bytes:
                            mask = np.frombuffer(mask_chunk, dtype=np.uint8).reshape(insert_h, insert_w, 3)
                            mask = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY)
                            if (insert_w, insert_h) != (side, side):
                                mask = cv2.resize(mask, (side, side), interpolation=cv2.INTER_LINEAR)
                            alpha = self._finish_alpha(mask, feather, erode_px=shrink)
                        else:
                            alpha = static_alpha if static_alpha is not None else self._procedural_alpha(side)
                    else:
                        alpha = static_alpha
                    blended = insert.astype(np.float32) * alpha + plate_region.astype(np.float32) * (1.0 - alpha)
                    frame[y : y + side, x : x + side] = np.clip(blended, 0, 255).astype(np.uint8)
                    frames_inserted += 1

                encoder.stdin.write(frame.tobytes())
                frame_index += 1
        finally:
            for proc in (plate_dec, insert_dec, mask_dec):
                if proc is not None:
                    proc.stdout.close()
                    proc.wait()
            encoder.stdin.close()
            encoder.wait()
        if encoder.returncode != 0 or not out_path.is_file():
            raise RuntimeError("ffmpeg failed while encoding the composited video.")
        return frames_inserted

    def _attach_audio(self, video_path: Path, plate_path: Path) -> Path:
        if (self.parameter_values.get("audio_source") or "plate") != "plate":
            return video_path
        ffmpeg, ffprobe = _ffmpeg_paths()
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(plate_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if "audio" not in (probe.stdout or ""):
            logger.warning("Plate has no audio stream; composited video will be silent.")
            return video_path
        muxed = Path(tempfile.gettempdir()) / f"faceprep_muxed_{uuid.uuid4().hex}.mp4"
        result = subprocess.run(
            [
                ffmpeg, "-y", "-i", str(video_path), "-i", str(plate_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy", "-shortest", str(muxed),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not muxed.is_file():
            raise RuntimeError(f"ffmpeg audio remux failed: {(result.stderr or '')[-400:]}")
        return muxed

    # -- Output directory + media input handling (copied from
    # hyperreal/heygen/avatar_video.py; node files are self-contained) ------

    def _save_copy_to_output_directory(self, data: bytes, filename: str) -> None:
        """Optionally write the video into the user-chosen folder; failures are reported, not fatal."""
        directory = (self.parameter_values.get("output_directory") or "").strip()
        if not directory:
            return
        try:
            dir_path = self._resolve_directory(directory)
            dir_path.mkdir(parents=True, exist_ok=True)
            base, ext = filename.rsplit(".", 1)
            target = dir_path / filename
            counter = 1
            while target.exists():
                target = dir_path / f"{base}_{counter}.{ext}"
                counter += 1
            target.write_bytes(data)
            self._saved_files.append(str(target))
        except Exception as e:
            logger.warning("Could not save composited video to %r", directory, exc_info=True)
            self._saved_files.append(f"FAILED to save into {directory}: {e}")

    def _resolve_directory(self, value: str) -> Path:
        if "{" in value:
            from griptape_nodes.common.macro_parser import ParsedMacro
            from griptape_nodes.retained_mode.events.project_events import (
                GetPathForMacroRequest,
                GetPathForMacroResultSuccess,
            )

            result = GriptapeNodes.handle_request(GetPathForMacroRequest(parsed_macro=ParsedMacro(value), variables={}))
            if isinstance(result, GetPathForMacroResultSuccess):
                return Path(result.absolute_path)
            raise ValueError(f"Could not resolve output directory macro {value!r}.")
        path = Path(value)
        if path.is_absolute():
            return path
        try:
            return Path(GriptapeNodes.ConfigManager().workspace_path) / path
        except Exception:
            return path

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
