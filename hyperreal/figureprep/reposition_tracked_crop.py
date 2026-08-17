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
PIX_FMTS = ["yuv420p", "yuv444p"]
BACKGROUNDS = ["black", "gray", "white", "green"]
BACKGROUND_BGR = {
    "black": (0, 0, 0),
    "gray": (128, 128, 128),
    "white": (255, 255, 255),
    "green": (0, 255, 0),
}


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


def _parse_region(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or value.get("schema") != REGION_SCHEMA:
        raise ValueError(
            f"region input is not a {REGION_SCHEMA} dict "
            "(connect Detect Figure Track's or Crop To Region's region output)."
        )
    return value


class RepositionTrackedCrop(SuccessFailureNode):
    """Place a crop-space video back into wide-frame coordinates over a flat background.

    The inverse of Crop To Region for pipelines that do NOT paste back onto the
    plate: feed it the generated character video (or its matte — black padding is
    exactly what a matte wants) plus the region dict, and it renders a full-frame
    video with the subject moving exactly where the tracked figure was. Finish in
    Resolve/Nuke over any background. Input is scaled to the recorded crop size
    first, so a generator returning different dimensions never breaks alignment.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/figureprep",
            "description": "Render a crop-space video (generated character or matte) back into wide-frame "
            "position over a flat background, using the recorded track.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="Crop-space video: the generated character clip, or its matte.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="region",
                input_types=["json"],
                type="json",
                tooltip="Region dict from Detect Figure Track (or Detect Head Region).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_scale",
                input_types=["float"],
                type="float",
                default_value=1.0,
                tooltip="Render the canvas at this multiple of the plate size (2.0 = 4K UHD from an HD track). "
                "Generate at crop size x this scale and the clip lands 1:1 with no resampling — "
                "e.g. a 640x1080 crop generated at 1280x2160, repositioned at 2.0, keeps every generated pixel.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="background",
                type="str",
                default_value="black",
                tooltip="Flat fill outside the tracked window. black is correct for mattes and for "
                "comp sources you will matte in Resolve.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=BACKGROUNDS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="crf",
                input_types=["int"],
                type="int",
                default_value=12,
                tooltip="x264 CRF. 12 is near-lossless on purpose — this is a comp source, not a deliverable.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="pix_fmt",
                type="str",
                default_value="yuv420p",
                tooltip="4:4:4 preserves chroma into the comp but chokes some tools — try 420 first.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=PIX_FMTS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the repositioned clip into, e.g. {project_dir}/outputs. "
                "Leave empty to skip saving a file copy.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="repositioned_video",
                output_type="VideoUrlArtifact",
                tooltip="Full-frame video with the crop placed at the tracked positions.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="region_out",
                output_type="json",
                tooltip="The region dict, passed through unchanged.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the reposition result",
            result_details_placeholder="Reposition details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temp_path: Path | None = None
        out_path: Path | None = None
        try:
            region = _parse_region(self.parameter_values.get("region"))
            source_path, temp_path = self._artifact_to_local_file(self.parameter_values.get("video"), "video")
            crf = int(self.parameter_values.get("crf") or 12)
            pix_fmt = self.parameter_values.get("pix_fmt") or "yuv420p"
            background = self.parameter_values.get("background") or "black"
            output_scale = float(self.parameter_values.get("output_scale") or 1.0)
            if output_scale <= 0:
                output_scale = 1.0

            out_path = Path(tempfile.gettempdir()) / f"figureprep_repositioned_{uuid.uuid4().hex}.mp4"
            frames_done, in_dims, scaled, out_dims = self._reposition(
                source_path, region, out_path, crf, pix_fmt, background, output_scale
            )

            data = out_path.read_bytes()
            filename = f"figureprep_repositioned_{uuid.uuid4().hex[:8]}.mp4"
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
            self._saved_files: list[str] = []
            self._save_copy_to_output_directory(data, filename)
            self.parameter_output_values["repositioned_video"] = VideoUrlArtifact(value=saved_url, name=filename)
            self.parameter_output_values["region_out"] = region

            source = region["source"]
            warnings = []
            if frames_done != source["frame_count"]:
                warnings.append(
                    f"Input has {frames_done} frame(s) but the track covers {source['frame_count']} — "
                    "check that the generator preserved the clip length."
                )
            scale_note = f" (input {in_dims[0]}x{in_dims[1]}, resampled)" if scaled else " (input placed 1:1)"
            warning_text = ("\nWARNING: " + "\nWARNING: ".join(warnings)) if warnings else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Placed {frames_done} frames{scale_note} onto a {out_dims[0]}x{out_dims[1]} "
                    f"{background} canvas at output_scale {output_scale:g}, "
                    f"{len(data) / (1024 * 1024):.1f} MB at CRF {crf} {pix_fmt}.{warning_text}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if out_path is not None:
                out_path.unlink(missing_ok=True)

    # -- Reposition ----------------------------------------------------------

    def _reposition(
        self,
        source_path: Path,
        region: dict,
        out_path: Path,
        crf: int,
        pix_fmt: str,
        background: str,
        output_scale: float,
    ) -> tuple[int, tuple[int, int], bool, tuple[int, int]]:
        """Returns (frames_done, input_dims, was_resampled, output_dims).

        With output_scale matching the generator's upsample of the crop (e.g. 2.0 for
        a 640x1080 crop generated at 1280x2160), the input lands on the canvas 1:1 —
        no resampling, every generated pixel kept.
        """
        ffmpeg, _ = _ffmpeg_paths()
        source = region["source"]
        offsets = region["offsets"]

        def _even(value: float) -> int:
            return max(2, int(round(value / 2.0)) * 2)

        out_w, out_h = _even(source["width"] * output_scale), _even(source["height"] * output_scale)
        place_w = min(out_w, _even(region["box"]["width"] * output_scale))
        place_h = min(out_h, _even(region["box"]["height"] * output_scale))

        cap = cv2.VideoCapture(str(source_path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video {source_path.name} with OpenCV.")
        in_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        in_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        needs_scale = (in_w, in_h) != (place_w, place_h)
        # AREA for shrinking, LANCZOS4 for enlarging — standard quality picks.
        interpolation = cv2.INTER_AREA if (in_w * in_h) > (place_w * place_h) else cv2.INTER_LANCZOS4

        base_canvas = np.empty((out_h, out_w, 3), dtype=np.uint8)
        base_canvas[:] = BACKGROUND_BGR.get(background, (0, 0, 0))

        encoder = subprocess.Popen(
            [
                ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{out_w}x{out_h}", "-r", f"{source['frame_rate']:.6f}",
                "-i", "-", "-an", "-c:v", "libx264", "-preset", "slow",
                "-crf", str(crf), "-pix_fmt", pix_fmt, *_encoder_color_args(source_path), str(out_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        frames_done = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if needs_scale:
                    frame = cv2.resize(frame, (place_w, place_h), interpolation=interpolation)
                x, y = offsets[min(frames_done, len(offsets) - 1)]
                x = min(int(round(x * output_scale)), out_w - place_w)
                y = min(int(round(y * output_scale)), out_h - place_h)
                canvas = base_canvas.copy()
                canvas[y : y + place_h, x : x + place_w] = frame
                encoder.stdin.write(canvas.tobytes())
                frames_done += 1
        finally:
            cap.release()
            encoder.stdin.close()
            encoder.wait()
        if encoder.returncode != 0 or not out_path.is_file():
            raise RuntimeError("ffmpeg failed while encoding the repositioned video.")
        if frames_done == 0:
            raise RuntimeError("Input video decoded zero frames.")
        return frames_done, (in_w, in_h), needs_scale, (out_w, out_h)

    # -- Output directory + media input handling (copied from
    # hyperreal/heygen/avatar_video.py; node files are self-contained) ------

    def _save_copy_to_output_directory(self, data: bytes, filename: str) -> None:
        """Optionally write the clip into the user-chosen folder; failures are reported, not fatal."""
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
            logger.warning("Could not save repositioned clip to %r", directory, exc_info=True)
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
