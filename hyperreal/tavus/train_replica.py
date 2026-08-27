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
DEFAULT_MODEL = "phoenix-3"


def _as_url(value: Any, label: str) -> str:
    """Accept a plain string or a URL artifact; Tavus needs a PUBLIC http(s) URL it can fetch."""
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} is empty. Wire the public URL from Upload to Spaces (or paste one).")
    url = raw.strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"{label} must be a public http(s) URL, got: {url[:80]}")
    if "localhost" in url or "127.0.0.1" in url:
        raise ValueError(
            f"{label} points at localhost ({url[:80]}). Tavus fetches the video from its servers — "
            "upload it first (Upload to Spaces with public=true) and pass that URL."
        )
    return url


def _optional_url(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


class TavusTrainReplica(SuccessFailureNode):
    """Submit a training video to Tavus to train a conversational replica.

    Replaces the n8n "Replica Training Manager": upload the training video somewhere
    public (Upload to Spaces), hand Tavus the URL, get back a replica_id. Training runs
    for hours on Tavus's side — poll it with Tavus Replica Status rather than waiting here.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/tavus",
            "description": "Start training a Tavus conversational replica from a public training-video URL.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="train_video_url",
                input_types=["str", "VideoUrlArtifact"],
                type="str",
                default_value="",
                tooltip="PUBLIC URL of the training video (wire Upload to Spaces 'url'). Tavus downloads it; "
                "local paths and localhost URLs will not work.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="replica_name",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Name shown in the Tavus dashboard.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="consent_video_url",
                input_types=["str", "VideoUrlArtifact"],
                type="str",
                default_value="",
                tooltip="Optional PUBLIC URL of a separate consent-statement video. Leave empty when the consent "
                "statement is spoken inside the training video itself.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="model_name",
                input_types=["str"],
                type="str",
                default_value=DEFAULT_MODEL,
                tooltip="Tavus replica model, e.g. phoenix-3 (current default).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="gaze_correction",
                type="bool",
                default_value=True,
                tooltip="Ask Tavus to correct the subject's gaze toward camera.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="background_green_screen",
                type="bool",
                default_value=False,
                tooltip="Tell Tavus the training video was shot on green screen so it can key it.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="callback_url",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional webhook Tavus calls when training finishes. Usually empty here — use "
                "Tavus Replica Status to check on demand instead.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="replica_id",
                output_type="str",
                tooltip="The new replica's id — wire into Tavus Replica Status, and keep it: it IS the replica.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="status",
                output_type="str",
                tooltip="Initial status reported by Tavus (normally 'started').",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="response",
                output_type="json",
                tooltip="Raw Tavus response for the submission.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the Tavus training submission",
            result_details_placeholder="Submission details will appear here.",
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

            body: dict[str, Any] = {
                "train_video_url": _as_url(self.parameter_values.get("train_video_url"), "train_video_url"),
                "properties": {
                    "gaze_correction": bool(self.parameter_values.get("gaze_correction")),
                    "background_green_screen": bool(self.parameter_values.get("background_green_screen")),
                },
            }
            name = (self.parameter_values.get("replica_name") or "").strip()
            if name:
                body["replica_name"] = name
            model = (self.parameter_values.get("model_name") or "").strip()
            if model:
                body["model_name"] = model
            if _optional_url(self.parameter_values.get("consent_video_url")):
                body["consent_video_url"] = _as_url(self.parameter_values.get("consent_video_url"), "consent_video_url")
            callback = (self.parameter_values.get("callback_url") or "").strip()
            if callback:
                body["callback_url"] = callback

            response = self._request("POST", "/v2/replicas", api_key, json_body=body)
            self._raise_for_api_error(response, "replica training submission")
            data = response.json() if response.content else {}
            replica_id = data.get("replica_id")
            if not replica_id:
                raise RuntimeError(f"Tavus accepted the request but returned no replica_id: {response.text[:300]}")
            status = data.get("status") or "started"

            self.parameter_output_values["replica_id"] = replica_id
            self.parameter_output_values["status"] = status
            self.parameter_output_values["response"] = data
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Training submitted: replica_id {replica_id} (status '{status}', model "
                    f"{model or DEFAULT_MODEL}{', name ' + name if name else ''}). Training takes hours on "
                    "Tavus's side — check progress with Tavus Replica Status. Keep the replica_id; it is the replica."
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

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
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
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
