from __future__ import annotations

import base64
import json
import logging
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

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
AUDIO_SOURCES = ["plate", "silent", "none"]


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
        raise ValueError(f"region input is not a {REGION_SCHEMA} dict (connect Detect Head Region's output).")
    return value


class CropToRegion(SuccessFailureNode):
    """Cut the head region out of a plate video as a square clip for face-swap prep.

    Integer offsets make the crop a pure pixel copy — no resampling, nothing to
    soften the face before the swap sees it. Audio is stripped.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/faceprep",
            "description": "Crop the detected head region out of a video as a square clip (swap intermediate).",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The plate video (same one Detect Head Region analyzed).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="region",
                input_types=["json"],
                type="json",
                tooltip="hyperreal.head_region/1 dict from Detect Head Region.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="crf",
                input_types=["int"],
                type="int",
                default_value=12,
                tooltip="x264 CRF. 12 is near-lossless on purpose — this is a swap intermediate, not a deliverable.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="pix_fmt",
                type="str",
                default_value="yuv420p",
                tooltip="4:4:4 preserves chroma into the swap but High 4:4:4 Predictive chokes some tools — "
                "try 420 first.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=PIX_FMTS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="audio_source",
                type="str",
                default_value="plate",
                tooltip="Face-swap tools often require an audio track. plate = copy the plate's audio "
                "(falls back to a silent track if the plate has none, so audio is always present); "
                "silent = always add a silent track; none = strip audio.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=AUDIO_SOURCES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the head clip into, e.g. {project_dir}/outputs. "
                "Leave empty to skip saving a file copy.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="head_video",
                output_type="VideoUrlArtifact",
                tooltip="The square head clip (audio stripped).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="region_out",
                output_type="json",
                tooltip="The region dict, passed through unchanged for Composite Region Back.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the crop result",
            result_details_placeholder="Crop details will appear here.",
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

            out_path = Path(tempfile.gettempdir()) / f"faceprep_head_{uuid.uuid4().hex}.mp4"
            if region["mode"] == "static":
                self._crop_static(source_path, region, out_path, crf, pix_fmt)
            else:
                self._crop_tracked(source_path, region, out_path, crf, pix_fmt)

            final_path, audio_note = self._attach_audio(out_path, source_path)
            data = final_path.read_bytes()
            if final_path != out_path:
                final_path.unlink(missing_ok=True)
            filename = f"faceprep_head_{uuid.uuid4().hex[:8]}.mp4"
            try:
                saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
            except Exception:
                logger.warning("Could not save %s to static files", filename, exc_info=True)
                raise
            self._saved_files: list[str] = []
            self._save_copy_to_output_directory(data, filename)
            self.parameter_output_values["head_video"] = VideoUrlArtifact(value=saved_url, name=filename)
            self.parameter_output_values["region_out"] = region

            box = region["box"]
            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Cropped {box['width']}x{box['height']} ({region['mode']}) head clip, "
                    f"{len(data) / (1024 * 1024):.1f} MB at CRF {crf} {pix_fmt}, audio: {audio_note}.{saved_note}"
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

    # -- Crop paths ---------------------------------------------------------

    def _crop_static(self, source_path: Path, region: dict, out_path: Path, crf: int, pix_fmt: str) -> None:
        ffmpeg, _ = _ffmpeg_paths()
        box = region["box"]
        result = subprocess.run(
            [
                ffmpeg, "-y", "-i", str(source_path),
                "-vf", f"crop={box['width']}:{box['height']}:{box['x']}:{box['y']}",
                "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", pix_fmt,
                "-an", str(out_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not out_path.is_file():
            raise RuntimeError(f"ffmpeg static crop failed: {(result.stderr or '')[-400:]}")

    def _crop_tracked(self, source_path: Path, region: dict, out_path: Path, crf: int, pix_fmt: str) -> None:
        """Decode -> numpy slice per frame at that frame's offset -> encode. Streamed, never in memory."""
        ffmpeg, _ = _ffmpeg_paths()
        source = region["source"]
        side = region["box"]["width"]
        offsets = region["offsets"]
        src_w, src_h = source["width"], source["height"]
        frame_bytes = src_w * src_h * 3

        decoder = subprocess.Popen(
            [ffmpeg, "-v", "error", "-i", str(source_path), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        encoder = subprocess.Popen(
            [
                ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{side}x{side}", "-r", f"{source['frame_rate']:.6f}",
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
                chunk = decoder.stdout.read(frame_bytes)
                if len(chunk) < frame_bytes:
                    break
                frame = np.frombuffer(chunk, dtype=np.uint8).reshape(src_h, src_w, 3)
                x, y = offsets[min(frames_done, len(offsets) - 1)]
                encoder.stdin.write(frame[y : y + side, x : x + side].tobytes())
                frames_done += 1
        finally:
            decoder.stdout.close()
            decoder.wait()
            encoder.stdin.close()
            encoder.wait()
        if encoder.returncode != 0 or not out_path.is_file():
            raise RuntimeError("ffmpeg failed while encoding the tracked crop.")
        if frames_done != source["frame_count"]:
            logger.warning(
                "Tracked crop decoded %d frames but region.source.frame_count is %d",
                frames_done,
                source["frame_count"],
            )

    # -- Audio ---------------------------------------------------------------

    def _has_audio(self, path: Path) -> bool:
        _, ffprobe = _ffmpeg_paths()
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return "audio" in (probe.stdout or "")

    def _attach_audio(self, video_path: Path, plate_path: Path) -> tuple[Path, str]:
        """Mux plate or silent audio onto the video-only crop; returns (path, note for result_details)."""
        mode = self.parameter_values.get("audio_source") or "plate"
        if mode == "none":
            return video_path, "none"
        use_plate = mode == "plate" and self._has_audio(plate_path)
        note = "copied from plate" if use_plate else ("silent track (plate has no audio)" if mode == "plate" else "silent track")

        ffmpeg, _ = _ffmpeg_paths()
        muxed = Path(tempfile.gettempdir()) / f"faceprep_head_audio_{uuid.uuid4().hex}.mp4"
        if use_plate:
            cmd = [
                ffmpeg, "-y", "-i", str(video_path), "-i", str(plate_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", str(muxed),
            ]
        else:
            cmd = [
                ffmpeg, "-y", "-i", str(video_path),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-map", "0:v:0", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-shortest", str(muxed),
            ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not muxed.is_file():
            raise RuntimeError(f"ffmpeg audio mux failed: {(result.stderr or '')[-400:]}")
        return muxed, note

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
            logger.warning("Could not save head clip to %r", directory, exc_info=True)
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
