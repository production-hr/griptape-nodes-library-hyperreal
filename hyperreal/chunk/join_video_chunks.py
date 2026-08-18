from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import requests
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")

DOWNLOAD_TIMEOUT_SECONDS = 600
JOIN_TIMEOUT_SECONDS = 3600


def _ffmpeg_paths() -> tuple[str, str]:
    """(ffmpeg, ffprobe) executables; static_ffmpeg downloads them on first use."""
    import static_ffmpeg.run

    return static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()


class JoinVideoChunks(SuccessFailureNode):
    """Concatenate a list of video chunks back into one clip, verifying the frame count.

    The companion to Split Video (Frame Accurate). Two reasons this exists rather than
    using the stock Concatenate Videos node:

    1. ForEach End outputs an untyped `list`, which the stock node's typed
       `list[VideoUrlArtifact]` input refuses — so a chunked loop cannot be wired at all.
    2. Splitting is only frame-exact if the join is too. This decodes the result and
       checks it against the sum of its parts, failing loudly on any drift.

    Chunks are stream-copied when they share codec parameters (the usual case when they
    came from one splitter), falling back to a re-encode when they do not.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/chunk",
            "description": "Join video chunks into one clip and verify the frame count.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="videos",
                input_types=["list", "list[VideoUrlArtifact]", "list[VideoArtifact]", "list[str]"],
                type="list",
                tooltip="The chunks to join, in order — e.g. straight from a ForEach End 'results'.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="expected_frames",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="If greater than 0, fail unless the joined clip decodes to exactly this many "
                "frames. Wire the splitter's total_frames here to prove the round trip.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also write the joined clip into, e.g. {project_dir}/outputs.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="video",
                output_type="VideoUrlArtifact",
                tooltip="The joined clip.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="total_frames",
                output_type="int",
                tooltip="Decoded frame count of the joined clip.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the join operation",
            result_details_placeholder="Join details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temps: list[Path] = []
        out_path: Path | None = None
        list_file: Path | None = None
        try:
            items = self.parameter_values.get("videos")
            if not items:
                raise ValueError("No videos connected to join.")
            if not isinstance(items, list):
                raise ValueError(f"Expected a list of videos, got {type(items).__name__}.")

            ffmpeg, ffprobe = _ffmpeg_paths()

            paths: list[Path] = []
            for i, item in enumerate(items):
                path, temp = self._artifact_to_local_file(item, f"videos[{i}]")
                paths.append(path)
                if temp is not None:
                    temps.append(temp)

            per_chunk = [self._decoded_frame_count(ffprobe, p) for p in paths]
            expected_sum = sum(per_chunk)

            out_path = Path(tempfile.gettempdir()) / f"joined_{uuid.uuid4().hex}.mp4"
            list_file = Path(tempfile.gettempdir()) / f"concat_{uuid.uuid4().hex}.txt"
            list_file.write_text(
                "".join(f"file '{p.as_posix()}'\n" for p in paths), encoding="utf-8"
            )

            mode = self._join(ffmpeg, list_file, out_path)
            actual = self._decoded_frame_count(ffprobe, out_path)

            expected_param = int(self.parameter_values.get("expected_frames") or 0)
            problems = []
            if actual != expected_sum:
                problems.append(f"joined clip has {actual} frames but the {len(paths)} chunks sum to {expected_sum}")
            if expected_param > 0 and actual != expected_param:
                problems.append(f"joined clip has {actual} frames but expected_frames is {expected_param}")
            if problems:
                raise RuntimeError("Frame-accuracy check failed:\n  " + "\n  ".join(problems))

            data = out_path.read_bytes()
            filename = f"joined_{uuid.uuid4().hex[:8]}.mp4"
            url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
            self._saved_files: list[str] = []
            self._save_copy_to_output_directory(data, filename)
            self.parameter_output_values["video"] = VideoUrlArtifact(value=url, name=filename)
            self.parameter_output_values["total_frames"] = actual

            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            counts = " + ".join(str(c) for c in per_chunk)
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Joined {len(paths)} chunks ({counts} = {actual} frames) via {mode}, "
                    f"{len(data) / (1024 * 1024):.1f} MB.{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            for p in (*temps, out_path, list_file):
                if p is not None:
                    p.unlink(missing_ok=True)

    # -- Joining -------------------------------------------------------------

    @staticmethod
    def _join(ffmpeg: str, list_file: Path, out_path: Path) -> str:
        """Stream copy if the chunks are compatible, else re-encode. Returns which was used."""
        copy_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(list_file),
                    "-c", "copy", "-an", str(out_path)]
        proc = subprocess.run(copy_cmd, capture_output=True, text=True, timeout=JOIN_TIMEOUT_SECONDS)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return "stream copy"
        logger.info("concat stream copy failed, re-encoding: %s", proc.stderr.strip()[:200])
        encode_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                      "-f", "concat", "-safe", "0", "-i", str(list_file),
                      "-fps_mode", "passthrough", "-an",
                      "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(out_path)]
        proc = subprocess.run(encode_cmd, capture_output=True, text=True, timeout=JOIN_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {proc.stderr.strip()[:600]}")
        return "re-encode"

    @staticmethod
    def _decoded_frame_count(ffprobe: str, path: Path) -> int:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=JOIN_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe failed on {path.name}: {proc.stderr.strip()[:300]}")
        for token in proc.stdout.split():
            token = token.strip().rstrip(",")
            if token.isdigit():
                return int(token)
        raise RuntimeError(f"Could not read a frame count from {path.name}.")

    # -- Output directory + media input handling (same pattern as
    # hyperreal/chunk/split_video_frame_accurate.py) --------------------------

    def _save_copy_to_output_directory(self, data: bytes, filename: str) -> None:
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
            logger.warning("Could not save joined clip to %r", directory, exc_info=True)
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
        value = getattr(artifact, "value", artifact)
        if isinstance(value, str) and value and not value.startswith(("http://", "https://", "data:")):
            path = self._resolve_workspace_path(value)
            if path is not None:
                return path, None
        data = self._artifact_to_bytes(artifact, label)
        handle = Path(tempfile.gettempdir()) / f"join_{uuid.uuid4().hex}.mp4"
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
