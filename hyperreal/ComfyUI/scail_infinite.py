from __future__ import annotations

import asyncio
import json
import logging
import os
import time
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

logger = logging.getLogger("griptape_nodes")

__all__ = ["RunComfyScailInfinite"]

API_BASE = "https://api.runcomfy.net/prod/v2"
SECRET_NAME = "RUNCOMFY_TOKEN"

SUCCESS_STATUSES = {"completed", "succeeded", "success", "done"}
FAILURE_STATUSES = {"failed", "error", "cancelled", "canceled", "timeout", "timed_out"}
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".gif", ".m4v", ".avi")


class RunComfyScailInfinite(ControlNode):
    """Run a RunComfy serverless (ComfyUI) deployment and return the generated video.

    Submits an inference request with per-node input overrides, polls the async
    queue until the job finishes, then extracts the output video URL. The poll
    loop is cancel-aware: pressing stop in the editor interrupts the wait and
    asks RunComfy to cancel the queued/running job.

    The API token is read from the ``RUNCOMFY_TOKEN`` secret (set it in Griptape
    settings or as an environment variable before launching the engine).

    Inputs:
        - deployment_id (str): RunComfy deployment id.
        - image_url (str): Reference image URL (mapped to image_node_id).
        - positive_text (str): Positive/subject text (mapped to positive_text_node_id).
        - motion_video_url (str): Motion/driving video URL (mapped to motion_video_node_id).
        - prompt_text (str): Main prompt text (mapped to prompt_node_id).
        - extra_overrides_json (str): Optional JSON merged into the overrides object.
        - poll_interval (int): Seconds between status polls.
        - timeout_seconds (int): Max seconds to wait before giving up.

    Outputs:
        - video_url (VideoUrlArtifact): The generated video URL.
        - request_id (str): RunComfy request id.
        - status (str): Final status string.
        - result_json (str): Full result payload as JSON text.
        - was_successful (bool): Whether the job completed successfully.
    """

    _NODE_ID_DEFAULTS: ClassVar[dict[str, str]] = {
        "image_node_id": "311",
        "positive_text_node_id": "568",
        "motion_video_node_id": "614",
        "prompt_node_id": "647",
        "lora_node_id": "463",
        "width_node_id": "330",
        "height_node_id": "331",
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        # ---- Inputs ----
        self.add_parameter(
            ParameterString(
                name="deployment_id",
                default_value="d5ced23b-11c9-49e3-bfe5-15f2589f6473",
                tooltip="RunComfy deployment id to run.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterString(
                name="image_url",
                default_value="",
                tooltip="Reference image URL (public HTTPS or data URI).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterString(
                name="positive_text",
                default_value="",
                tooltip="Positive / subject text override.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            ParameterString(
                name="motion_video_url",
                default_value="",
                tooltip="Motion / driving video URL (public HTTPS or data URI).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterString(
                name="prompt_text",
                default_value="",
                tooltip="Main prompt text override.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"multiline": True},
            )
        )

        # ---- Video size + optional LoRA ----
        self.add_parameter(
            ParameterInt(
                name="video_width",
                default_value=1080,
                tooltip="Output video width (px). 0 = leave workflow default.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterInt(
                name="video_height",
                default_value=1920,
                tooltip="Output video height (px). 0 = leave workflow default.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterString(
                name="lora_name",
                default_value="",
                tooltip="Optional LoRA filename (e.g. myLora.safetensors). Blank = no LoRA override.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterFloat(
                name="lora_strength",
                default_value=0.85,
                tooltip="LoRA model strength (applied only when lora_name is set).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

        # ---- Advanced (hidden by default) ----
        with ParameterGroup(name="Advanced") as advanced:
            for pname, default in self._NODE_ID_DEFAULTS.items():
                ParameterString(
                    name=pname,
                    default_value=default,
                    tooltip=f"ComfyUI node id for {pname.replace('_node_id', '')}.",
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                )
            ParameterString(
                name="extra_overrides_json",
                default_value="",
                tooltip='Optional JSON merged into "overrides", e.g. {"25": {"inputs": {"noise_seed": 42}}}.',
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"multiline": True},
            )
            ParameterInt(
                name="poll_interval",
                default_value=5,
                tooltip="Seconds between status polls.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
            ParameterInt(
                name="timeout_seconds",
                default_value=6000,
                tooltip="Maximum seconds to wait for completion (default 6000 = 100 min). On timeout the RunComfy job is cancelled.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        advanced.ui_options = {"collapsed": True}
        self.add_node_element(advanced)

        # ---- Outputs ----
        self.add_parameter(
            ParameterVideo(
                name="video_url",
                tooltip="Generated video as a URL artifact for downstream display/save.",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
                settable=False,
                ui_options={"pulse_on_run": True},
            )
        )
        self.add_parameter(
            ParameterString(
                name="request_id",
                tooltip="RunComfy request id.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            ParameterString(
                name="status",
                tooltip="Final job status.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            ParameterString(
                name="result_json",
                tooltip="Full result payload as JSON text.",
                allowed_modes={ParameterMode.OUTPUT},
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="was_successful",
                type="bool",
                output_type="bool",
                default_value=False,
                tooltip="Whether the job completed successfully.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    # ------------------------------------------------------------------ helpers

    def _get_token(self) -> str | None:
        token: str | None = None
        try:
            token = GriptapeNodes.SecretsManager().get_secret(SECRET_NAME)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s SecretsManager lookup failed: %s", self.name, exc)
        if not token:
            token = os.environ.get(SECRET_NAME)
        return token

    def _cancel_requested(self) -> bool:
        return bool(getattr(self, "is_cancellation_requested", False))

    def _build_overrides(self) -> dict[str, Any]:
        overrides: dict[str, Any] = {}

        def _put(node_id_param: str, input_key: str, value: str) -> None:
            node_id = (self.get_parameter_value(node_id_param) or "").strip()
            if node_id and value:
                overrides[node_id] = {"inputs": {input_key: value}}

        _put("image_node_id", "image", (self.get_parameter_value("image_url") or "").strip())
        _put("positive_text_node_id", "text", self.get_parameter_value("positive_text") or "")
        _put("motion_video_node_id", "video", (self.get_parameter_value("motion_video_url") or "").strip())
        _put("prompt_node_id", "text", self.get_parameter_value("prompt_text") or "")

        # Video size (PrimitiveInt nodes use the "value" input). 0 = skip.
        def _put_int(node_id_param: str, value: Any) -> None:
            node_id = (self.get_parameter_value(node_id_param) or "").strip()
            try:
                ivalue = int(value)
            except (TypeError, ValueError):
                return
            if node_id and ivalue > 0:
                overrides[node_id] = {"inputs": {"value": ivalue}}

        _put_int("width_node_id", self.get_parameter_value("video_width"))
        _put_int("height_node_id", self.get_parameter_value("video_height"))

        # Optional LoRA (name + strength on a LoraLoaderModelOnly node).
        lora_name = (self.get_parameter_value("lora_name") or "").strip()
        if lora_name:
            lora_node_id = (self.get_parameter_value("lora_node_id") or "").strip()
            try:
                strength = float(self.get_parameter_value("lora_strength"))
            except (TypeError, ValueError):
                strength = 0.85
            if lora_node_id:
                overrides[lora_node_id] = {"inputs": {"lora_name": lora_name, "strength_model": strength}}

        extra_raw = (self.get_parameter_value("extra_overrides_json") or "").strip()
        if extra_raw:
            try:
                extra = json.loads(extra_raw)
                if isinstance(extra, dict):
                    overrides.update(extra)
                else:
                    logger.warning("%s extra_overrides_json is not a JSON object; ignoring.", self.name)
            except json.JSONDecodeError as exc:
                logger.warning("%s could not parse extra_overrides_json: %s", self.name, exc)

        return overrides

    @staticmethod
    def _extract_video_url(result: dict[str, Any]) -> str | None:
        outputs = result.get("outputs") or {}
        candidates: list[str] = []
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            for items in node_output.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and item.get("url"):
                        candidates.append(str(item["url"]))
        if not candidates:
            return None
        for url in candidates:
            path = urlparse(url).path.lower()
            if path.endswith(VIDEO_EXTS):
                return url
        return candidates[0]

    async def _sleep_cancellable(self, client: httpx.AsyncClient, seconds: int, cancel_url: str, auth: dict) -> bool:
        """Sleep in 1s steps; if cancellation is requested, cancel the job and return True."""
        for _ in range(max(1, seconds)):
            if self._cancel_requested():
                await self._cancel_job(client, cancel_url, auth)
                return True
            await asyncio.sleep(1)
        return False

    async def _cancel_job(self, client: httpx.AsyncClient, cancel_url: str, auth: dict) -> None:
        if not cancel_url:
            return
        try:
            await client.post(cancel_url, headers=auth, timeout=30)
            logger.info("%s requested RunComfy job cancellation.", self.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s failed to send cancel request: %s", self.name, exc)

    # ------------------------------------------------------------------ execute

    def validate_before_node_run(self) -> list[Exception] | None:
        exceptions: list[Exception] = []
        if not self._get_token():
            exceptions.append(
                ValueError(
                    f"{self.name}: RunComfy API token not found. Set the '{SECRET_NAME}' secret in "
                    "Griptape settings or as an environment variable."
                )
            )
        if not (self.get_parameter_value("deployment_id") or "").strip():
            exceptions.append(ValueError(f"{self.name}: deployment_id is required."))
        return exceptions or None

    async def aprocess(self) -> None:
        token = self._get_token()
        deployment_id = (self.get_parameter_value("deployment_id") or "").strip()
        poll_interval = max(1, int(self.get_parameter_value("poll_interval") or 5))
        timeout_seconds = max(10, int(self.get_parameter_value("timeout_seconds") or 6000))
        max_poll_failures = 10

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        auth = {"Authorization": f"Bearer {token}"}
        overrides = self._build_overrides()

        submit_url = f"{API_BASE}/deployments/{deployment_id}/inference"
        cancel_url = ""

        async with httpx.AsyncClient() as client:
            # 1) Submit
            logger.info("%s submitting inference (%d overrides)", self.name, len(overrides))
            try:
                resp = await client.post(submit_url, headers=headers, json={"overrides": overrides}, timeout=60)
            except httpx.HTTPError as exc:
                self._fail(status="submit_error", detail=str(exc))
                return
            if resp.status_code >= 400:
                self._fail(status="submit_error", detail=f"HTTP {resp.status_code}: {resp.text[:500]}")
                return

            submit_json = resp.json()
            request_id = submit_json.get("request_id", "")
            status_url = submit_json.get("status_url") or (
                f"{API_BASE}/deployments/{deployment_id}/requests/{request_id}/status"
            )
            result_url = submit_json.get("result_url") or (
                f"{API_BASE}/deployments/{deployment_id}/requests/{request_id}/result"
            )
            cancel_url = submit_json.get("cancel_url") or (
                f"{API_BASE}/deployments/{deployment_id}/requests/{request_id}/cancel"
            )
            self.parameter_output_values["request_id"] = request_id

            # 2) Poll status (cancel-aware)
            deadline = time.time() + timeout_seconds
            last_status = "in_queue"
            poll_failures = 0
            while time.time() < deadline:
                if await self._sleep_cancellable(client, poll_interval, cancel_url, auth):
                    self._fail(status="cancelled", detail="Cancelled by user; RunComfy job cancellation requested.")
                    return
                try:
                    s = await client.get(status_url, headers=auth, timeout=60)
                except httpx.HTTPError as exc:
                    poll_failures += 1
                    logger.warning(
                        "%s status poll error %d/%d (will retry): %s", self.name, poll_failures, max_poll_failures, exc
                    )
                    if poll_failures >= max_poll_failures:
                        await self._cancel_job(client, cancel_url, auth)
                        self._fail(
                            status="poll_failed",
                            detail=f"Status polling failed {poll_failures} times in a row ({exc}); cancelled the RunComfy job.",
                        )
                        return
                    continue
                if s.status_code >= 400:
                    poll_failures += 1
                    logger.warning(
                        "%s status HTTP %s %d/%d (will retry): %s",
                        self.name,
                        s.status_code,
                        poll_failures,
                        max_poll_failures,
                        s.text[:200],
                    )
                    if poll_failures >= max_poll_failures:
                        await self._cancel_job(client, cancel_url, auth)
                        self._fail(
                            status="poll_failed",
                            detail=f"Status endpoint returned errors {poll_failures} times in a row "
                            f"(last HTTP {s.status_code}); cancelled the RunComfy job.",
                        )
                        return
                    continue
                poll_failures = 0
                sjson = s.json()
                last_status = str(sjson.get("status", "")).lower()
                logger.info("%s status: %s", self.name, last_status or "<none>")
                if last_status in SUCCESS_STATUSES:
                    break
                if last_status in FAILURE_STATUSES:
                    self._fail(status=last_status, detail=json.dumps(sjson)[:1000])
                    return
            else:
                await self._cancel_job(client, cancel_url, auth)
                self._fail(
                    status="timeout",
                    detail=f"No terminal status within {timeout_seconds}s (last={last_status}); cancelled the RunComfy job.",
                )
                return

            # 3) Fetch result
            try:
                r = await client.get(result_url, headers=auth, timeout=120)
                result = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                self._fail(status="result_error", detail=str(exc))
                return

        self.parameter_output_values["result_json"] = json.dumps(result, indent=2)
        result_status = str(result.get("status", last_status)).lower()
        video_url = self._extract_video_url(result)

        if video_url:
            filename = f"runcomfy_{request_id or 'output'}.mp4"
            self.parameter_output_values["video_url"] = VideoUrlArtifact(value=video_url, name=filename)
            self.parameter_output_values["status"] = result_status or "completed"
            self.parameter_output_values["was_successful"] = True
            logger.info("%s completed: %s", self.name, video_url)
        else:
            self._fail(status=result_status or "no_output", detail="Job finished but no video URL found in outputs.")

    def _fail(self, status: str, detail: str) -> None:
        self.parameter_output_values["video_url"] = None
        self.parameter_output_values["status"] = status
        self.parameter_output_values["was_successful"] = False
        existing = self.parameter_output_values.get("result_json") or ""
        self.parameter_output_values["result_json"] = existing or detail
        logger.error("%s failed (%s): %s", self.name, status, detail)
