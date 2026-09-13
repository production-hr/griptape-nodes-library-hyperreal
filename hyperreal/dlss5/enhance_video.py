from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterGroup, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.exe_types.param_types.parameter_float import ParameterFloat
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.exe_types.param_types.parameter_video import ParameterVideo
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

__all__ = ["Dlss5EnhanceVideo"]

# Ports are probed in this order, so an already-open ComfyUI Desktop (8000) is
# reused before anything new is launched.
CANDIDATE_PORTS = (8000, 8188)

UPSCALING_MODES = [
    "1x (DLAA / native)",
    "1.5x (Quality)",
    "1.724x (Balanced)",
    "2x (Performance)",
    "3x (Ultra Performance)",
]
MODEL_PRESETS = ["Default", "J", "K", "L", "M"]
NR_PRESETS = ["Default", "Preset #1", "Preset #2", "Preset #3"]
NR_STYLES = ["Default", "Natural", "Cinematic"]
MOTION_MODES = ["auto", "optical_flow", "none"]
CODECS = ["H.264", "HEVC", "AV1", "ProRes Proxy"]
CONTAINERS = ["MP4", "MKV", "MOV"]
QUALITIES = ["Auto", "Good", "Best", "Max"]

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi")


class Dlss5EnhanceVideo(ControlNode):
    """Run NVIDIA DLSS 5 neural rendering over a video using a local ComfyUI.

    Wraps ``DLSS5 Enhance Video File`` from ``Blueforcer/ComfyUI-DLSS5-Enhancer``.
    Unlike the RunComfy nodes in this library, DLSS 5 has to execute locally: the
    runtime drives the GPU directly through a native worker, so there is no
    hosted equivalent to call.

    Defaults here are the ones that measured best on HyperReal footage rather
    than the node pack's own:

      * ``local_tone_strength`` is **0.0**, not 1.0. At 1.0 the enhancer applies
        a local tone map that crushes highlights and desaturates by ~18%; that,
        not the neural pass, is the entire colour shift. At 0.0 it is colour
        neutral.
      * ``dlss_model_preset`` is **M**, which retains the most texture.
      * ``upscaling_mode`` is **1x**. The other modes do nothing on the
        community (RTX 40-series) runtime, which falls back to native.

    Two things decide whether this pass helps at all, and neither is a setting:

      * **Motion.** Static and slow shots gain detail; fast-motion shots lose it.
        On fast motion set ``motion`` to ``none`` to limit the damage, or skip
        the pass entirely.
      * **Resolution.** At HD the result is roughly parity; at 4K it returns
        ~2.1x the detail for ~1.46x the shimmer. Upscale *before* this node.

    Inputs:
        - video (VideoUrlArtifact | str): Source video, artifact or local path.
        - dlss_model_preset (str): Default, J, K, L or M.
        - local_tone_strength (float): 0.0 keeps colour neutral.
        - motion (str): auto, optical_flow or none.
        - upscaling_mode (str): DLSS mode; 1x on 40-series.
        - codec / container / quality (str): Encoder settings for the output.
        - output_directory (str): Empty writes to ComfyUI's output folder.

    Outputs:
        - video (VideoUrlArtifact): The enhanced video.
        - output_path (str): Absolute path of the file the node wrote.
        - frames (int): Frames processed.
        - status (str): Final status string.
        - was_successful (bool): Whether the run completed.
    """

    _DESKTOP_PYTHONS: ClassVar[tuple[str, ...]] = (r"%USERPROFILE%\Documents\ComfyUI\.venv\Scripts\python.exe",)
    _DESKTOP_MAINS: ClassVar[tuple[str, ...]] = (r"%LOCALAPPDATA%\Programs\ComfyUI\resources\ComfyUI\main.py",)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        # ---- Inputs ----
        self.add_parameter(
            ParameterVideo(
                name="video",
                input_types=["VideoUrlArtifact", "str"],
                type="VideoUrlArtifact",
                tooltip="Source video. Upscale before this node: DLSS 5 gains far more at 4K than at HD.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterString(
                name="dlss_model_preset",
                default_value="M",
                traits={Options(choices=MODEL_PRESETS)},
                tooltip="DLSS model. M retains the most skin and hair texture; Default/J/K are softest.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterFloat(
                name="local_tone_strength",
                default_value=0.0,
                tooltip=(
                    "Local tone mapping. Keep at 0.0. The node pack ships 1.0, where it crushes "
                    "highlights and desaturates by ~18% - the whole colour shift comes from here."
                ),
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterString(
                name="motion",
                default_value="auto",
                traits={Options(choices=MOTION_MODES)},
                tooltip=(
                    "Temporal accumulation. auto suits static and slow shots. On fast motion the "
                    "optical flow misregisters and destroys detail - use none there, or skip the pass."
                ),
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterString(
                name="upscaling_mode",
                default_value=UPSCALING_MODES[0],
                traits={Options(choices=UPSCALING_MODES)},
                tooltip="Leave at 1x on RTX 40-series: the community runtime rejects the other modes.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterString(
                name="output_directory",
                default_value="",
                tooltip="Absolute path to write into. Empty writes to ComfyUI's output folder.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

        # ---- Encoding ----
        with ParameterGroup(name="Encoding") as encoding:
            ParameterString(
                name="codec",
                default_value="H.264",
                traits={Options(choices=CODECS)},
                tooltip="H.264 and HEVC use NVENC with a software fallback.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterString(
                name="container",
                default_value="MP4",
                traits={Options(choices=CONTAINERS)},
                tooltip="MKV stream-copies audio and subtitles; MP4 and MOV re-encode audio to AAC.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterString(
                name="quality",
                default_value="Good",
                traits={Options(choices=QUALITIES)},
                tooltip=(
                    "Max is constant quality and very large (~8 MB per second of footage); use it "
                    "when measuring. Good is the sensible production setting."
                ),
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            Parameter(
                name="copy_audio",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip="Mux the original audio into the result.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        encoding.ui_options = {"collapsed": True}
        self.add_node_element(encoding)

        # ---- Advanced ----
        with ParameterGroup(name="Advanced") as advanced:
            ParameterFloat(
                name="local_structure_strength",
                default_value=1.5,
                tooltip="Detail and structure reconstruction. Measured effect between 1.5 and 2.0 is negligible.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterFloat(
                name="skin_structure_strength",
                default_value=2.0,
                tooltip="Skin and pore reconstruction. Requires automatic_mask.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            Parameter(
                name="automatic_mask",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip="Let the model detect skin regions. Gate for skin_structure_strength.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterString(
                name="nr_style",
                default_value="Default",
                traits={Options(choices=NR_STYLES)},
                tooltip="Look of the neural pass.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterString(
                name="nr_preset",
                default_value="Default",
                traits={Options(choices=NR_PRESETS)},
                tooltip="Measured to produce identical output at every value on current builds.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterFloat(
                name="nr_intensity",
                default_value=1.0,
                tooltip="Strength of the neural pass. Above 1.0 has no further effect.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterFloat(
                name="scene_change_threshold",
                default_value=0.24,
                tooltip="Measured to make no difference between 0.24 and 0.90.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterInt(
                name="max_frames",
                default_value=0,
                tooltip="0 renders the whole video; any other value renders a preview of that many frames.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterString(
                name="server_url",
                default_value="",
                tooltip=(
                    "ComfyUI base URL. Empty probes http://127.0.0.1:8000 then :8188 and, if "
                    "auto_start_server is on, launches a headless server when neither answers."
                ),
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            Parameter(
                name="auto_start_server",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip=(
                    "Launch a headless ComfyUI if none is reachable. The process outlives this node "
                    "and is reused by later runs; close it yourself when you are done."
                ),
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterString(
                name="comfy_python",
                default_value="",
                tooltip="Python executable for auto-start. Empty auto-detects the ComfyUI Desktop venv.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterString(
                name="comfy_main",
                default_value="",
                tooltip="ComfyUI main.py for auto-start. Empty auto-detects the ComfyUI Desktop install.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterInt(
                name="timeout_seconds",
                default_value=3600,
                tooltip="Maximum seconds to wait for the render.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        advanced.ui_options = {"collapsed": True}
        self.add_node_element(advanced)

        # ---- Outputs ----
        self.add_parameter(
            ParameterVideo(
                name="video_out",
                tooltip="Enhanced video.",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
                settable=False,
                ui_options={"pulse_on_run": True},
            )
        )
        self.add_parameter(
            ParameterString(
                name="output_path",
                tooltip="Absolute path of the file DLSS 5 wrote.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            ParameterInt(
                name="frames",
                default_value=0,
                tooltip="Frames processed.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            ParameterString(
                name="status",
                tooltip="Final status string.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="was_successful",
                type="bool",
                output_type="bool",
                default_value=False,
                tooltip="Whether the render completed successfully.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    # ------------------------------------------------------------------ helpers

    def _resolve_source(self) -> Path:
        """Get a local path for the input, downloading only when we must.

        ComfyUI runs on this machine, so a local file can be handed over by path
        and never has to pass through memory.
        """
        value = self.get_parameter_value("video")
        raw = getattr(value, "value", value)
        if not raw:
            msg = "video is empty. Connect a video or enter a local path."
            raise ValueError(msg)

        if isinstance(raw, (bytes, bytearray)):
            tmp = Path(tempfile.gettempdir()) / f"dlss5_{uuid.uuid4().hex}.mp4"
            tmp.write_bytes(bytes(raw))
            return tmp

        text = str(raw).strip().strip('"').strip("'")
        parsed = urlparse(text)

        if parsed.scheme == "file":
            return Path(parsed.path.lstrip("/")).resolve()

        if parsed.scheme in ("http", "https"):
            suffix = Path(parsed.path).suffix or ".mp4"
            tmp = Path(tempfile.gettempdir()) / f"dlss5_{uuid.uuid4().hex}{suffix}"
            logger.info("%s downloading source to %s", self.name, tmp)
            with httpx.stream("GET", text, timeout=120, follow_redirects=True) as r:
                r.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
            return tmp

        path = Path(text).expanduser()
        if not path.is_file():
            msg = f"No video file at {path}"
            raise FileNotFoundError(msg)
        return path.resolve()

    @staticmethod
    async def _alive(client: httpx.AsyncClient, base: str) -> bool:
        try:
            r = await client.get(f"{base}/system_stats", timeout=3)
        except httpx.HTTPError:
            return False
        return r.status_code < 400

    def _detect(self, candidates: tuple[str, ...]) -> str:
        for raw in candidates:
            path = Path(os.path.expandvars(raw))
            if path.is_file():
                return str(path)
        return ""

    def _spawn_server(self, port: int) -> None:
        """Launch a detached headless ComfyUI that survives this node."""
        python = (self.get_parameter_value("comfy_python") or "").strip() or self._detect(self._DESKTOP_PYTHONS)
        main = (self.get_parameter_value("comfy_main") or "").strip() or self._detect(self._DESKTOP_MAINS)
        if not python or not main:
            msg = (
                "No ComfyUI reachable and auto-start could not find one. Set comfy_python and "
                "comfy_main on this node, or start ComfyUI yourself."
            )
            raise RuntimeError(msg)

        base_dir = Path(main).parents[0]
        desktop_base = Path(os.path.expandvars(r"%USERPROFILE%\Documents\ComfyUI"))
        front_end = base_dir / "web_custom_versions" / "desktop_app"

        args = [python, "-s", main, "--listen", "127.0.0.1", "--port", str(port), "--log-stdout"]
        if desktop_base.is_dir():
            args += [
                "--base-directory",
                str(desktop_base),
                "--user-directory",
                str(desktop_base / "user"),
                "--input-directory",
                str(desktop_base / "input"),
                "--output-directory",
                str(desktop_base / "output"),
            ]
        if front_end.is_dir():
            # Without this, a Desktop install refuses to boot: its frontend is bundled
            # rather than installed as the comfyui-frontend-package wheel.
            args += ["--front-end-root", str(front_end)]

        log = Path(tempfile.gettempdir()) / f"dlss5_comfyui_{port}.log"
        logger.info("%s launching ComfyUI on port %d, log at %s", self.name, port, log)
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        with log.open("ab") as fh:
            subprocess.Popen(  # noqa: S603 - paths come from node parameters
                args,
                stdout=fh,
                stderr=fh,
                stdin=subprocess.DEVNULL,
                creationflags=creation,
                close_fds=True,
            )

    async def _ensure_server(self, client: httpx.AsyncClient) -> str:
        explicit = (self.get_parameter_value("server_url") or "").strip().rstrip("/")
        if explicit:
            if await self._alive(client, explicit):
                return explicit
            msg = f"No ComfyUI answering at {explicit}."
            raise RuntimeError(msg)

        for port in CANDIDATE_PORTS:
            base = f"http://127.0.0.1:{port}"
            if await self._alive(client, base):
                logger.info("%s using ComfyUI already running at %s", self.name, base)
                return base

        if not bool(self.get_parameter_value("auto_start_server")):
            msg = "No ComfyUI answering on 127.0.0.1:8000 or :8188. Start ComfyUI, or turn on auto_start_server."
            raise RuntimeError(msg)

        port = CANDIDATE_PORTS[-1]
        base = f"http://127.0.0.1:{port}"
        self._spawn_server(port)
        deadline = time.time() + 180
        while time.time() < deadline:
            await asyncio.sleep(3)
            if await self._alive(client, base):
                logger.info("%s ComfyUI ready at %s", self.name, base)
                return base
        msg = f"Launched ComfyUI but it did not become ready within 180s. See {tempfile.gettempdir()}."
        raise RuntimeError(msg)

    async def _require_nodes(self, client: httpx.AsyncClient, base: str) -> None:
        r = await client.get(f"{base}/object_info", timeout=60)
        r.raise_for_status()
        if "DLSS5EnhanceVideoFile" not in r.json():
            msg = (
                f"The ComfyUI at {base} does not have ComfyUI-DLSS5-Enhancer installed. Clone it "
                "into custom_nodes, run install_runtime.py, and restart ComfyUI."
            )
            raise RuntimeError(msg)

    def _graph(self, source: Path) -> dict[str, Any]:
        get = self.get_parameter_value
        return {
            "1": {
                "class_type": "DLSS5Settings",
                "inputs": {
                    "upscaling_mode": get("upscaling_mode"),
                    "nr_preset": get("nr_preset"),
                    "nr_style": get("nr_style"),
                    "nr_intensity": float(get("nr_intensity")),
                    "local_tone_strength": float(get("local_tone_strength")),
                    "local_structure_strength": float(get("local_structure_strength")),
                    "skin_structure_strength": float(get("skin_structure_strength")),
                    "automatic_mask": bool(get("automatic_mask")),
                    "dlss_model_preset": get("dlss_model_preset"),
                    "motion": get("motion"),
                    "scene_change_threshold": float(get("scene_change_threshold")),
                    "warmup_frames": 0,
                    "runtime_dir": "",
                },
            },
            "2": {
                "class_type": "DLSS5EnhanceVideoFile",
                "inputs": {
                    "video_path": str(source),
                    "settings": ["1", 0],
                    "codec": get("codec"),
                    "container": get("container"),
                    "quality": get("quality"),
                    "filename_prefix": f"{source.stem[:40]}_DLSS5",
                    "output_directory": (get("output_directory") or "").strip(),
                    "max_frames": int(get("max_frames") or 0),
                    "copy_audio": bool(get("copy_audio")),
                    "verify_neural_rendering": True,
                },
            },
        }

    @staticmethod
    def _parse_result(entry: dict[str, Any]) -> tuple[str, int]:
        """Pull the written path and frame count out of the node's UI text."""
        texts = entry.get("outputs", {}).get("2", {}).get("text", [])
        for line in texts:
            text = str(line)
            if " (" in text and text.rstrip().endswith("frames)"):
                path, _, tail = text.rpartition(" (")
                count = tail.split()[0]
                return path.strip(), int(count) if count.isdigit() else 0
            if text.lower().endswith(VIDEO_EXTS):
                return text.strip(), 0
        return "", 0

    def _fail(self, status: str, detail: str) -> None:
        logger.error("%s %s: %s", self.name, status, detail)
        self.parameter_output_values["video_out"] = None
        self.parameter_output_values["output_path"] = ""
        self.parameter_output_values["frames"] = 0
        self.parameter_output_values["status"] = f"{status}: {detail}"
        self.parameter_output_values["was_successful"] = False

    # ------------------------------------------------------------------ run

    async def aprocess(self) -> None:
        timeout_seconds = max(30, int(self.get_parameter_value("timeout_seconds") or 3600))

        try:
            source = self._resolve_source()
        except (ValueError, OSError, httpx.HTTPError) as exc:
            self._fail("input_error", str(exc))
            return

        async with httpx.AsyncClient() as client:
            try:
                base = await self._ensure_server(client)
                await self._require_nodes(client, base)
            except (RuntimeError, httpx.HTTPError) as exc:
                self._fail("server_error", str(exc))
                return

            payload = {"prompt": self._graph(source), "client_id": uuid.uuid4().hex}
            try:
                r = await client.post(f"{base}/prompt", json=payload, timeout=60)
            except httpx.HTTPError as exc:
                self._fail("submit_error", str(exc))
                return
            if r.status_code >= 400:
                self._fail("submit_error", f"HTTP {r.status_code}: {r.text[:800]}")
                return

            prompt_id = r.json().get("prompt_id", "")
            logger.info("%s queued %s on %s", self.name, prompt_id, base)
            self.parameter_output_values["status"] = "running"

            started = time.time()
            entry: dict[str, Any] | None = None
            while time.time() - started < timeout_seconds:
                try:
                    await asyncio.sleep(2)
                except asyncio.CancelledError:
                    await self._interrupt(client, base)
                    self._fail("cancelled", "Cancelled by user; ComfyUI interrupt requested.")
                    raise
                try:
                    h = await client.get(f"{base}/history/{prompt_id}", timeout=30)
                    history = h.json() if h.status_code < 400 else {}
                except httpx.HTTPError:
                    continue
                if prompt_id in history:
                    entry = history[prompt_id]
                    break

            if entry is None:
                await self._interrupt(client, base)
                self._fail("timeout", f"No result after {timeout_seconds}s.")
                return

        status = entry.get("status", {})
        if not status.get("success", True):
            detail = ""
            for kind, body in status.get("messages", []):
                if kind == "execution_error":
                    detail = json.dumps(body)[:800]
            self._fail("execution_error", detail or "ComfyUI reported a failure.")
            return

        path_text, frames = self._parse_result(entry)
        produced = Path(path_text) if path_text else None
        if not produced or not produced.is_file():
            self._fail("missing_output", f"ComfyUI reported {path_text!r} but no file is there.")
            return

        try:
            data = produced.read_bytes()
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, produced.name)
        except OSError as exc:
            self._fail("save_error", str(exc))
            return

        elapsed = time.time() - started
        logger.info("%s wrote %s (%d frames) in %.1fs", self.name, produced, frames, elapsed)
        self.parameter_output_values["video_out"] = VideoUrlArtifact(value=saved_url, name=produced.name)
        self.parameter_output_values["output_path"] = str(produced)
        self.parameter_output_values["frames"] = frames
        self.parameter_output_values["status"] = "completed"
        self.parameter_output_values["was_successful"] = True

    async def _interrupt(self, client: httpx.AsyncClient, base: str) -> None:
        try:
            await client.post(f"{base}/interrupt", timeout=10)
        except httpx.HTTPError as exc:
            logger.warning("%s could not interrupt ComfyUI: %s", self.name, exc)
