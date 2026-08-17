from __future__ import annotations

import base64
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

logger = logging.getLogger("griptape_nodes")

DOWNLOAD_TIMEOUT_SECONDS = 600


def _ffmpeg_paths() -> tuple[str, str]:
    """(ffmpeg, ffprobe) executables; static_ffmpeg downloads them on first use."""
    import static_ffmpeg.run

    return static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()


def _guided_filter(guide: np.ndarray, src: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """He et al. guided filter via box filters — edge-aware smoothing of src steered by guide.

    Both inputs float32 in [0, 1], single channel. O(1) per pixel regardless of radius.
    """
    ksize = (2 * radius + 1, 2 * radius + 1)
    mean_i = cv2.boxFilter(guide, -1, ksize)
    mean_p = cv2.boxFilter(src, -1, ksize)
    corr_i = cv2.boxFilter(guide * guide, -1, ksize)
    corr_ip = cv2.boxFilter(guide * src, -1, ksize)
    var_i = corr_i - mean_i * mean_i
    cov_ip = corr_ip - mean_i * mean_p
    a = cov_ip / (var_i + eps)
    b = mean_p - a * mean_i
    mean_a = cv2.boxFilter(a, -1, ksize)
    mean_b = cv2.boxFilter(b, -1, ksize)
    return mean_a * guide + mean_b


class RefineVideoMatte(SuccessFailureNode):
    """Turn a hard, jagged matte video into a soft edge-accurate alpha, guided by the RGB.

    Built for SAM3's output: temporally stable but hard-binary silhouettes. The guided
    filter lets the RGB clip's real edges (hair, motion blur, fabric) shape the alpha
    within a band around the mask boundary — SAM3's stability with soft-matte edges.
    Works on any white-on-black matte source (keyer output, Magic Mask exports, RMBG).
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/matte",
            "description": "Refine a hard matte video into a soft edge-accurate alpha using the RGB clip as guide.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="matte_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The hard matte to refine (white = subject), e.g. SAM3 Segment output.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="rgb_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The color clip the matte belongs to — its edges guide the refinement. "
                "Must be frame-locked to the matte (same clip the matte was extracted from).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="radius_px",
                input_types=["int"],
                type="int",
                default_value=8,
                tooltip="Refinement band half-width in pixels. Bigger = softer, wider edge transition; "
                "8-12 suits 1080-2160px content.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="edge_softness",
                input_types=["float"],
                type="float",
                default_value=0.001,
                tooltip="Guided-filter epsilon. Smaller hugs image edges harder (crisper); larger smooths "
                "more. Typical range 0.0001-0.01.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_shift_px",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Grow (+) or choke (-) the hard matte before refining. Choke a couple px when the "
                "source matte overshoots into background.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="in_black",
                input_types=["float"],
                type="float",
                default_value=0.05,
                tooltip="Alpha levels: values at or below this become 0 (cleans background haze).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="in_white",
                input_types=["float"],
                type="float",
                default_value=0.95,
                tooltip="Alpha levels: values at or above this become 1 (solidifies the core).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the refined matte into, e.g. {project_dir}/outputs. "
                "Leave empty to skip saving a file copy.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="refined_matte",
                output_type="VideoUrlArtifact",
                tooltip="The soft-alpha matte video (white = subject), near-lossless encode.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the matte refinement result",
            result_details_placeholder="Refinement details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temp_matte: Path | None = None
        temp_rgb: Path | None = None
        out_path: Path | None = None
        try:
            matte_path, temp_matte = self._artifact_to_local_file(self.parameter_values.get("matte_video"), "matte_video")
            rgb_path, temp_rgb = self._artifact_to_local_file(self.parameter_values.get("rgb_video"), "rgb_video")
            radius = max(1, int(self.parameter_values.get("radius_px") or 8))
            eps = max(1e-6, float(self.parameter_values.get("edge_softness") or 0.001))
            shift = int(self.parameter_values.get("matte_shift_px") or 0)
            in_black = float(self.parameter_values.get("in_black") or 0.0)
            in_white = float(self.parameter_values.get("in_white") or 1.0)
            if in_white <= in_black:
                raise ValueError(f"in_white ({in_white}) must be greater than in_black ({in_black}).")

            out_path = Path(tempfile.gettempdir()) / f"matte_refined_{uuid.uuid4().hex}.mp4"
            frames_done, mismatch_note = self._refine(
                matte_path, rgb_path, out_path, radius, eps, shift, in_black, in_white
            )

            data = out_path.read_bytes()
            filename = f"matte_refined_{uuid.uuid4().hex[:8]}.mp4"
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
            self._saved_files: list[str] = []
            self._save_copy_to_output_directory(data, filename)
            self.parameter_output_values["refined_matte"] = VideoUrlArtifact(value=saved_url, name=filename)

            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            warning = f"\nWARNING: {mismatch_note}" if mismatch_note else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Refined {frames_done} matte frames (radius {radius}px, eps {eps:g}, shift {shift:+d}px, "
                    f"levels {in_black:g}-{in_white:g}), {len(data) / (1024 * 1024):.1f} MB.{warning}{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            for p in (temp_matte, temp_rgb, out_path):
                if p is not None:
                    p.unlink(missing_ok=True)

    # -- Refinement ----------------------------------------------------------

    def _refine(
        self,
        matte_path: Path,
        rgb_path: Path,
        out_path: Path,
        radius: int,
        eps: float,
        shift: int,
        in_black: float,
        in_white: float,
    ) -> tuple[int, str]:
        ffmpeg, _ = _ffmpeg_paths()
        mcap = cv2.VideoCapture(str(matte_path))
        rcap = cv2.VideoCapture(str(rgb_path))
        if not mcap.isOpened() or not rcap.isOpened():
            raise RuntimeError("Could not open matte or RGB video with OpenCV.")
        rw, rh = int(rcap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(rcap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(rcap.get(cv2.CAP_PROP_FPS)) or 25.0
        mcount, rcount = int(mcap.get(cv2.CAP_PROP_FRAME_COUNT)), int(rcap.get(cv2.CAP_PROP_FRAME_COUNT))
        mismatch_note = ""
        if mcount and rcount and mcount != rcount:
            mismatch_note = (
                f"Matte has {mcount} frames but RGB has {rcount} — refining the overlapping "
                "frames only. The matte should be extracted from this exact clip."
            )

        kernel = None
        if shift != 0:
            k = 2 * abs(shift) + 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))

        encoder = subprocess.Popen(
            [
                ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "gray",
                "-s", f"{rw}x{rh}", "-r", f"{fps:.6f}",
                "-i", "-", "-an", "-c:v", "libx264", "-preset", "slow",
                "-crf", "10", "-pix_fmt", "yuv420p", str(out_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        frames_done = 0
        scale = 1.0 / (in_white - in_black)
        try:
            while True:
                ok_m, mf = mcap.read()
                ok_r, rf = rcap.read()
                if not ok_m or not ok_r:
                    break
                matte = cv2.cvtColor(mf, cv2.COLOR_BGR2GRAY) if mf.ndim == 3 else mf
                if matte.shape[:2] != (rh, rw):
                    matte = cv2.resize(matte, (rw, rh), interpolation=cv2.INTER_LINEAR)
                p = matte.astype(np.float32) / 255.0
                if kernel is not None:
                    p = cv2.dilate(p, kernel) if shift > 0 else cv2.erode(p, kernel)
                guide = cv2.cvtColor(rf, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
                q = _guided_filter(guide, p, radius, eps)
                q = np.clip((q - in_black) * scale, 0.0, 1.0)
                encoder.stdin.write((q * 255.0 + 0.5).astype(np.uint8).tobytes())
                frames_done += 1
        finally:
            mcap.release()
            rcap.release()
            encoder.stdin.close()
            encoder.wait()
        if encoder.returncode != 0 or not out_path.is_file():
            raise RuntimeError("ffmpeg failed while encoding the refined matte.")
        if frames_done == 0:
            raise RuntimeError("No overlapping frames decoded from matte and RGB videos.")
        return frames_done, mismatch_note

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
            logger.warning("Could not save refined matte to %r", directory, exc_info=True)
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
        handle = Path(tempfile.gettempdir()) / f"matte_{label}_{uuid.uuid4().hex}.mp4"
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
