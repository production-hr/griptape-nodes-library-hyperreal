from __future__ import annotations

import logging
import time
from typing import Any

import requests
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")

API_BASE = "https://tavusapi.com"
API_KEY_NAME = "TAVUS_API_KEY"
REQUEST_TIMEOUT_SECONDS = 60
TERMINAL_OK = {"completed", "ready"}
TERMINAL_FAILED = {"error", "failed"}


class TavusReplicaStatus(SuccessFailureNode):
    """Check (or wait on) a Tavus replica's training status by replica_id.

    Replaces the callback webhook in the old n8n flow: run it whenever you want a
    status, or set wait_until_complete to block until training finishes. On 'error'
    the node fails with Tavus's error_message so the reason is visible on the canvas.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/tavus",
            "description": "Fetch a Tavus replica's training status/progress, optionally waiting until it completes.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="replica_id",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="The replica id from Tavus Train Replica (or the Tavus dashboard).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="wait_until_complete",
                type="bool",
                default_value=False,
                tooltip="Keep polling until training completes or errors (training takes hours). Off = one check.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="poll_interval_seconds",
                input_types=["int"],
                type="int",
                default_value=60,
                tooltip="Seconds between polls when waiting.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="max_wait_minutes",
                input_types=["int"],
                type="int",
                default_value=360,
                tooltip="Give up waiting after this long (the replica keeps training; just check again later).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        for out_name, tip in (
            ("status", "Tavus training status, e.g. started / completed / error."),
            ("training_progress", "Progress string as reported by Tavus (e.g. '40/100')."),
            ("replica_name", "Replica name as registered with Tavus."),
            ("thumbnail_video_url", "Preview clip URL once Tavus provides one."),
            ("error_message", "Tavus error text when training failed (empty otherwise)."),
        ):
            self.add_parameter(
                Parameter(name=out_name, output_type="str", tooltip=tip, allowed_modes={ParameterMode.OUTPUT})
            )
        self.add_parameter(
            Parameter(
                name="replica",
                output_type="json",
                tooltip="Full replica record from Tavus.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="status_summary",
                output_type="str",
                tooltip="Ready-to-send plain-text summary of the status — wire into Send Email 'body'.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the Tavus replica status check",
            result_details_placeholder="Status details will appear here.",
        )

    def validate_before_node_run(self) -> list[Exception] | None:
        try:
            api_key = GriptapeNodes.SecretsManager().get_secret(API_KEY_NAME)
        except Exception as e:
            return [e]
        if not api_key:
            return [
                ValueError(
                    f"{API_KEY_NAME} is not set. Add it under Settings > API Keys & Secrets, "
                    "or set it as an environment variable."
                )
            ]
        return None

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        try:
            api_key = GriptapeNodes.SecretsManager().get_secret(API_KEY_NAME)
            if not api_key:
                raise ValueError(f"{API_KEY_NAME} is not set.")
            replica_id = (self.parameter_values.get("replica_id") or "").strip()
            if not replica_id:
                raise ValueError("replica_id is empty. Wire it from Tavus Train Replica or paste it in.")

            wait = bool(self.parameter_values.get("wait_until_complete"))
            interval = max(5, int(self.parameter_values.get("poll_interval_seconds") or 60))
            deadline = time.monotonic() + 60 * max(1, int(self.parameter_values.get("max_wait_minutes") or 360))

            polls = 0
            while True:
                record = self._fetch(api_key, replica_id)
                polls += 1
                status = str(record.get("status") or "").lower()
                if not wait or status in TERMINAL_OK or status in TERMINAL_FAILED:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(interval)

            self._publish(record)
            status = str(record.get("status") or "")
            progress = str(record.get("training_progress") or "")
            if status.lower() in TERMINAL_FAILED:
                raise RuntimeError(
                    f"Tavus replica {replica_id} training failed: "
                    f"{record.get('error_message') or 'no error_message provided'}"
                )
            timed_out = wait and status.lower() not in TERMINAL_OK
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Replica {replica_id}: status '{status}'"
                    f"{' (' + progress + ')' if progress else ''}"
                    f"{', name ' + str(record.get('replica_name')) if record.get('replica_name') else ''}"
                    f" after {polls} check(s)."
                    + (" Still training when max_wait_minutes elapsed — run this node again later." if timed_out else "")
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    def _publish(self, record: dict) -> None:
        status = str(record.get("status") or "")
        progress = str(record.get("training_progress") or "")
        error = str(record.get("error_message") or "")
        self.parameter_output_values["status"] = status
        self.parameter_output_values["training_progress"] = progress
        self.parameter_output_values["replica_name"] = str(record.get("replica_name") or "")
        self.parameter_output_values["thumbnail_video_url"] = str(record.get("thumbnail_video_url") or "")
        self.parameter_output_values["error_message"] = error
        self.parameter_output_values["replica"] = record
        headline = {
            "completed": "Tavus replica training COMPLETED",
            "ready": "Tavus replica training COMPLETED",
            "error": "Tavus replica training FAILED",
            "failed": "Tavus replica training FAILED",
        }.get(status.lower(), f"Tavus replica training status: {status or 'unknown'}")
        self.parameter_output_values["status_summary"] = (
            f"{headline}\n"
            f"\n"
            f"Replica name: {record.get('replica_name') or '(unnamed)'}\n"
            f"Replica ID: {record.get('replica_id') or ''}\n"
            f"Status: {status}\n"
            f"Progress: {progress or 'n/a'}\n"
            f"Preview: {record.get('thumbnail_video_url') or '(not yet available)'}\n"
            f"Error: {error or 'none'}\n"
            f"Checked: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def _fetch(self, api_key: str, replica_id: str) -> dict:
        response = self._request("GET", f"/v2/replicas/{replica_id}?verbose=true", api_key)
        self._raise_for_api_error(response, "replica status")
        data = response.json() if response.content else {}
        return data if isinstance(data, dict) else {"raw": data}

    # -- HTTP helpers (self-contained by library convention) ------------------

    def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        json_body: dict | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> requests.Response:
        headers = {"x-api-key": api_key}
        response = None
        for _ in range(4):
            response = requests.request(method, f"{API_BASE}{path}", json=json_body, headers=headers, timeout=timeout)
            if response.status_code != 429:
                break
            try:
                retry_after = float(response.headers.get("Retry-After", "5"))
            except ValueError:
                retry_after = 5.0
            logger.warning("Tavus rate limit hit on %s; retrying in %.0fs", path, retry_after)
            time.sleep(min(retry_after, 60.0))
        assert response is not None
        return response

    def _raise_for_api_error(self, response: requests.Response, context: str) -> None:
        if response.ok:
            return
        if response.status_code in (401, 403):
            raise RuntimeError(f"Tavus rejected the API key ({context}). Check {API_KEY_NAME} in your settings.")
        if response.status_code == 404:
            raise RuntimeError(f"Tavus has no replica with that id ({context}). Check replica_id.")
        message = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                message = body.get("message") or body.get("error") or body.get("detail") or ""
                if isinstance(message, dict):
                    message = message.get("message") or str(message)
        except Exception:
            message = response.text[:300]
        raise RuntimeError(f"Tavus API error during {context} (HTTP {response.status_code}): {message}")
