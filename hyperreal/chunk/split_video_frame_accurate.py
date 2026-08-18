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
SPLIT_TIMEOUT_SECONDS = 3600


def _ffmpeg_paths() -> tuple[str, str]:
    """(ffmpeg, ffprobe) executables; static_ffmpeg downloads them on first use."""
    import static_ffmpeg.run

    return static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()


class SplitVideoFrameAccurate(SuccessFailureNode):
    """Split a video into chunks on exact frame boundaries, verifying every chunk.

    Built because time-based splitting (ffmpeg `-ss` with a stream copy, and the stock
    Split Video node) snaps to keyframes: chunks silently overlap, overshoot their
    requested range, or drop the tail frame. That is invisible in frame counts — two
    errors can cancel — and fatal for matte work, where a duplicated or missing frame
    desynchronises the matte from the plate for the rest of the timeline.

    Every chunk is cut with an explicit frame-range select filter, re-encoded, and then
    verified by decoding it back: if a chunk's decoded frame count does not equal
    `end - start + 1`, the node fails loudly rather than handing on a bad chunk.

    Audio is dropped — this exists to feed frame-accurate processing, not editorial.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/chunk",
            "description": "Split a video into frame-exact chunks, verified by decode.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The video to split.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="mode",
                input_types=["str"],
                type="str",
                default_value="auto chunks",
                tooltip="'auto chunks' divides the clip by max_frames_per_chunk. "
                "'explicit ranges' uses the frame_ranges field.",
                allowed_modes={ParameterMode.PROPERTY},
                ui_options={"simple_dropdown": ["auto chunks", "explicit ranges"]},
            )
        )
        self.add_parameter(
            Parameter(
                name="max_frames_per_chunk",
                input_types=["int"],
                type="int",
                default_value=300,
                tooltip="Auto mode: largest chunk to emit. Size this from VRAM — SAM3 costs roughly a "
                "fixed base plus ~8 MB/frame at 1080p-class, ~12.7 MB/frame at 4K.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="overlap_frames",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Auto mode: frames each chunk repeats from the previous one, for blending seams. "
                "0 tiles the source exactly with no duplicates.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="frame_ranges",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Explicit mode: one INCLUSIVE 'start-end' per line (0-based), e.g. '0-304'. "
                "Optionally '0-304|Label'.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"multiline": True, "placeholder_text": "0-304\n305-609"},
            )
        )
        self.add_parameter(
            Parameter(
                name="quality_crf",
                input_types=["int"],
                type="int",
                default_value=14,
                tooltip="x264 CRF for the chunks. 14 is visually lossless; lower is bigger. Chunks are "
                "re-encoded because frame-exact cutting cannot be done with a stream copy.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also write the chunks into, e.g. {project_dir}/outputs. "
                "Leave empty to skip saving file copies.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="chunks",
                output_type="list[VideoUrlArtifact]",
                tooltip="The frame-exact chunks, in order.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="frame_ranges_used",
                output_type="str",
                tooltip="The inclusive ranges actually cut, one per line — feed this to the reassembly "
                "step or keep it with the render for provenance.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="num_chunks",
                output_type="int",
                tooltip="How many chunks were produced.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="total_frames",
                output_type="int",
                tooltip="Decoded frame count of the source (authoritative, not container metadata).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the split operation",
            result_details_placeholder="Split details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temp_src: Path | None = None
        out_paths: list[Path] = []
        try:
            src_path, temp_src = self._artifact_to_local_file(self.parameter_values.get("video"), "video")
            ffmpeg, ffprobe = _ffmpeg_paths()

            total = self._decoded_frame_count(ffprobe, src_path)
            if total <= 0:
                raise ValueError(f"Could not determine a frame count for {src_path.name}.")

            mode = (self.parameter_values.get("mode") or "auto chunks").strip()
            if mode == "explicit ranges":
                ranges = self._parse_ranges(self.parameter_values.get("frame_ranges") or "", total)
            else:
                per = max(1, int(self.parameter_values.get("max_frames_per_chunk") or 300))
                overlap = max(0, int(self.parameter_values.get("overlap_frames") or 0))
                if overlap >= per:
                    raise ValueError(f"overlap_frames ({overlap}) must be smaller than max_frames_per_chunk ({per}).")
                ranges = self._plan_chunks(total, per, overlap)

            crf = int(self.parameter_values.get("quality_crf") or 14)
            out_paths = self._cut(ffmpeg, src_path, ranges, crf)

            report = self._verify(ffprobe, ranges, out_paths, total)

            artifacts: list[VideoUrlArtifact] = []
            self._saved_files: list[str] = []
            for (start, end), path in zip(ranges, out_paths, strict=True):
                data = path.read_bytes()
                filename = f"chunk_{start:06d}_{end:06d}_{uuid.uuid4().hex[:8]}.mp4"
                url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
                self._save_copy_to_output_directory(data, filename)
                artifacts.append(VideoUrlArtifact(value=url, name=filename))

            ranges_text = "\n".join(f"{a}-{b}" for a, b in ranges)
            self.parameter_output_values["chunks"] = artifacts
            self.parameter_output_values["frame_ranges_used"] = ranges_text
            self.parameter_output_values["num_chunks"] = len(artifacts)
            self.parameter_output_values["total_frames"] = total

            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Split {total} frames into {len(ranges)} verified chunks (CRF {crf}).\n{report}{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            if temp_src is not None:
                temp_src.unlink(missing_ok=True)
            for p in out_paths:
                p.unlink(missing_ok=True)

    # -- Planning ------------------------------------------------------------

    @staticmethod
    def _plan_chunks(total: int, per: int, overlap: int) -> list[tuple[int, int]]:
        """Inclusive [start, end] ranges covering 0..total-1. With overlap 0 they tile exactly."""
        ranges: list[tuple[int, int]] = []
        start = 0
        while start < total:
            end = min(start + per - 1, total - 1)
            ranges.append((start, end))
            if end >= total - 1:
                break
            start = end + 1 - overlap
        return ranges

    @staticmethod
    def _parse_ranges(text: str, total: int) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split("|", 1)[0].strip()
            if not line:
                continue
            if "-" not in line:
                raise ValueError(f"frame_ranges line {lineno}: expected 'start-end', got {raw!r}.")
            a_text, _, b_text = line.partition("-")
            try:
                a, b = int(a_text), int(b_text)
            except ValueError as exc:
                raise ValueError(f"frame_ranges line {lineno}: non-numeric range {raw!r}.") from exc
            if a < 0 or b < a:
                raise ValueError(f"frame_ranges line {lineno}: invalid range {a}-{b}.")
            if b > total - 1:
                raise ValueError(f"frame_ranges line {lineno}: end {b} is past the last frame ({total - 1}).")
            ranges.append((a, b))
        if not ranges:
            raise ValueError("explicit ranges mode selected but frame_ranges is empty.")
        return ranges

    # -- Cutting -------------------------------------------------------------

    def _cut(self, ffmpeg: str, src: Path, ranges: list[tuple[int, int]], crf: int) -> list[Path]:
        """One decode pass, N encoded outputs, each selected by absolute frame number."""
        tmp = Path(tempfile.gettempdir())
        outs = [tmp / f"chunk_{uuid.uuid4().hex}.mp4" for _ in ranges]
        labels = [f"c{i}" for i in range(len(ranges))]

        chains = [f"[0:v]split={len(ranges)}" + "".join(f"[s{i}]" for i in range(len(ranges)))]
        for i, (a, b) in enumerate(ranges):
            chains.append(f"[s{i}]select='between(n\\,{a}\\,{b})',setpts=PTS-STARTPTS[{labels[i]}]")
        filter_complex = ";".join(chains)

        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-filter_complex", filter_complex]
        for label, out in zip(labels, outs, strict=True):
            cmd += ["-map", f"[{label}]", "-fps_mode", "passthrough", "-an",
                    "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", str(out)]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SPLIT_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg split failed: {proc.stderr.strip()[:600]}")
        return outs

    # -- Verification --------------------------------------------------------

    def _verify(
        self, ffprobe: str, ranges: list[tuple[int, int]], outs: list[Path], total: int
    ) -> str:
        lines = []
        problems = []
        covered = 0
        for (a, b), path in zip(ranges, outs, strict=True):
            expected = b - a + 1
            actual = self._decoded_frame_count(ffprobe, path)
            covered += actual
            ok = actual == expected
            lines.append(f"  {a}-{b}: expected {expected}, got {actual} {'OK' if ok else 'MISMATCH'}")
            if not ok:
                problems.append(f"chunk {a}-{b} expected {expected} frames but decoded {actual}")
        contiguous = all(ranges[i + 1][0] == ranges[i][1] + 1 for i in range(len(ranges) - 1))
        if contiguous and ranges[0][0] == 0 and ranges[-1][1] == total - 1 and covered != total:
            problems.append(f"chunks cover {covered} frames but the source has {total}")
        if problems:
            raise RuntimeError("Frame-accuracy check failed:\n  " + "\n  ".join(problems))
        footer = f"  total {covered} frames across {len(ranges)} chunks (source {total})"
        return "\n".join([*lines, footer])

    @staticmethod
    def _decoded_frame_count(ffprobe: str, path: Path) -> int:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=SPLIT_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe failed on {path.name}: {proc.stderr.strip()[:300]}")
        for token in proc.stdout.split():
            token = token.strip().rstrip(",")
            if token.isdigit():
                return int(token)
        raise RuntimeError(f"Could not read a frame count from {path.name}.")

    # -- Output directory + media input handling (same pattern as
    # hyperreal/matte/refine_video_matte.py; node files are self-contained) ---

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
            logger.warning("Could not save chunk to %r", directory, exc_info=True)
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
        handle = Path(tempfile.gettempdir()) / f"split_{label}_{uuid.uuid4().hex}.mp4"
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
