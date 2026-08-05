from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

API_BASE = "https://api.topazlabs.com"
API_KEY_NAME = "TOPAZ_API_KEY"
REQUEST_TIMEOUT_SECONDS = 60
DOWNLOAD_TIMEOUT_SECONDS = 600
UPLOAD_TIMEOUT_SECONDS = 900
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # Topaz express request cap
MAX_WAIT_SECONDS = 60 * 60
INITIAL_POLL_SECONDS = 10.0
MAX_POLL_SECONDS = 30.0

# Model id -> panel label. Filters are sent with the model id only; Topaz's
# auto-parameter mode tunes everything else.
MODELS = {
    "prob-4": "Proteus (general upscaling)",
    "iris-3": "Iris (face recovery)",
    "ahq-12": "Artemis HQ",
    "nyx-3": "Nyx (denoise)",
}
# Frame-interpolation model id -> panel label. "none" = plain resampling via
# output.frameRate only (fine for downward conversions like 60 -> 24).
FRAME_INTERPOLATION_MODELS = {
    "none": "None (resample only)",
    "chr-2": "Chronos (rate conversion, e.g. 24<->25<->30)",
    "chf-3": "Chronos Fast",
    "apo-8": "Apollo (big multipliers, e.g. ->60 / slow-mo)",
    "apf-2": "Apollo Fast",
}
VIDEO_ENCODERS = ["H264", "H265"]
TERMINAL_FAILURE_STATUSES = {"failed", "canceled", "canceling"}


class TopazVideoUpscale(SuccessFailureNode):
    """Upscale a video via the Topaz Labs Video API (express flow).

    Express mode needs no source metadata: create the request, PUT the bytes to
    the returned upload URL, poll status, then download the result within its TTL.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/topaz",
            "description": "Upscale and/or retime a video with Topaz Labs (Proteus, Iris, Artemis, Nyx; Chronos/Apollo frame interpolation).",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The video to upscale (mp4/mov/mkv, up to 500 MB).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="model",
                type="str",
                default_value="prob-4",
                tooltip="prob-4 = Proteus, the general-purpose upscaler. iris-3 = Iris, face recovery "
                "(best for talking-head avatars). ahq-12 = Artemis HQ. nyx-3 = Nyx, denoise.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=list(MODELS.keys()))},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_width",
                input_types=["int"],
                type="int",
                default_value=3840,
                tooltip="Output width in pixels. For portrait (9:16) video use e.g. 2160.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_height",
                input_types=["int"],
                type="int",
                default_value=2160,
                tooltip="Output height in pixels. For portrait (9:16) video use e.g. 3840.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_frame_rate",
                input_types=["int"],
                type="int",
                default_value=25,
                tooltip="Output frame rate. Match the source to avoid resampling — HeyGen outputs 25 fps. "
                "With a frame_interpolation model set, this is the AI-interpolated target rate.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="frame_interpolation",
                type="str",
                default_value="none",
                tooltip="AI frame interpolation to reach output_frame_rate. none = plain resampling "
                "(fine for downward conversions like 60->24). chr-2/chf-3 = Chronos, best for rate "
                "conversions like 24<->25<->30. apo-8/apf-2 = Apollo, best for big jumps like ->60. "
                "Fast variants trade quality for speed.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=list(FRAME_INTERPOLATION_MODELS.keys()))},
            )
        )
        self.add_parameter(
            Parameter(
                name="video_encoder",
                type="str",
                default_value="H264",
                tooltip="Output codec. H264 plays everywhere; H265 gives smaller 4K files.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=VIDEO_ENCODERS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the upscaled video into, e.g. {project_dir}/outputs. "
                "Leave empty to skip saving a file copy.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="upscaled_video",
                output_type="VideoUrlArtifact",
                tooltip="The upscaled video.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="request_id",
                output_type="str",
                tooltip="Topaz request id, for support and debugging.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the Topaz upscale result",
            result_details_placeholder="Upscale details will appear here.",
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
                    "then restart the engine (newly registered secrets only load at startup)."
                )
            ]
        return None

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        started = time.monotonic()
        self._saved_files: list[str] = []
        try:
            api_key = GriptapeNodes.SecretsManager().get_secret(API_KEY_NAME)
            if not api_key:
                raise ValueError(f"{API_KEY_NAME} is not set.")

            data = self._artifact_to_bytes(self.parameter_values.get("video"), "video")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"The video is {len(data) / (1024 * 1024):.0f} MB, over Topaz's 500 MB request limit."
                )

            request_id, upload_url = self._create_express_request(api_key, data)
            self.parameter_output_values["request_id"] = request_id
            logger.info("Topaz request %s created; uploading %d bytes", request_id, len(data))

            self._upload_source(upload_url, data)
            logger.info("Topaz request %s uploaded; polling for completion", request_id)

            detail = self._poll_request(api_key, request_id)
            download = detail.get("download") or {}
            download_url = download.get("url")
            if not download_url:
                raise RuntimeError(f"Topaz reported request {request_id} complete but returned no download URL.")

            artifact = self._save_video(request_id, download_url)
            self.parameter_output_values["upscaled_video"] = artifact

            elapsed = int(time.monotonic() - started)
            cost = (detail.get("estimates") or {}).get("cost")
            cost_note = f", ~{cost[0]} credits billed" if isinstance(cost, list) and cost else ""
            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Request {request_id} upscaled to {detail.get('outputSize') or 'target resolution'} "
                    f"in {elapsed}s{cost_note}.{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    # -- Topaz API helpers --------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        json_body: dict | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> requests.Response:
        response = None
        for _ in range(4):
            response = requests.request(
                method, f"{API_BASE}{path}", json=json_body, headers={"X-API-Key": api_key}, timeout=timeout
            )
            if response.status_code != 429:
                break
            try:
                retry_after = float(response.headers.get("Retry-After", "5"))
            except ValueError:
                retry_after = 5.0
            logger.warning("Topaz rate limit hit on %s; retrying in %.0fs", path, retry_after)
            time.sleep(min(retry_after, 60.0))
        assert response is not None
        return response

    def _raise_for_api_error(self, response: requests.Response, context: str) -> None:
        if response.ok:
            return
        if response.status_code == 401:
            raise RuntimeError(f"Topaz rejected the API key ({context}). Check {API_KEY_NAME} in your settings.")
        message = ""
        try:
            body = response.json()
            message = body.get("message") or body.get("error") or ""
            if isinstance(message, dict):
                message = message.get("message") or str(message)
        except Exception:
            message = response.text[:300]
        raise RuntimeError(f"Topaz API error during {context} (HTTP {response.status_code}): {message}")

    def _create_express_request(self, api_key: str, data: bytes) -> tuple[str, str]:
        container = self._guess_container(data)
        frame_rate = int(self.parameter_values.get("output_frame_rate") or 25)
        filters: list[dict[str, Any]] = [{"model": self.parameter_values.get("model") or "prob-4"}]
        interpolation = self.parameter_values.get("frame_interpolation") or "none"
        if interpolation != "none":
            filters.append({"model": interpolation, "fps": frame_rate})
        body: dict[str, Any] = {
            "source": {"container": container},
            "filters": filters,
            "output": {
                "resolution": {
                    "width": int(self.parameter_values.get("output_width") or 3840),
                    "height": int(self.parameter_values.get("output_height") or 2160),
                },
                "frameRate": frame_rate,
                "audioCodec": "AAC",
                "audioTransfer": "Copy",
                "videoEncoder": self.parameter_values.get("video_encoder") or "H264",
                "dynamicCompressionLevel": "High",
                "container": "mp4",
            },
        }
        response = self._request("POST", "/video/express", api_key, json_body=body)
        self._raise_for_api_error(response, "request creation")
        payload = response.json()
        request_id = payload.get("requestId") or payload.get("requestID")
        upload_urls = payload.get("uploadUrls") or []
        if not request_id or not upload_urls:
            raise RuntimeError(f"Topaz express request returned no requestId/uploadUrls: {response.text[:300]}")
        return request_id, upload_urls[0]

    def _upload_source(self, upload_url: str, data: bytes) -> None:
        response = requests.put(
            upload_url, data=data, headers={"Content-Type": "video/mp4"}, timeout=UPLOAD_TIMEOUT_SECONDS
        )
        if not response.ok:
            raise RuntimeError(f"Uploading the source video to Topaz failed (HTTP {response.status_code}).")

    def _poll_request(self, api_key: str, request_id: str) -> dict:
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        interval = INITIAL_POLL_SECONDS
        while time.monotonic() < deadline:
            response = self._request("GET", f"/video/{request_id}/status", api_key)
            self._raise_for_api_error(response, "status polling")
            detail = response.json()
            status = (detail.get("status") or "").lower()
            if status == "complete":
                return detail
            if status in TERMINAL_FAILURE_STATUSES:
                message = detail.get("message") or "No failure message provided."
                raise RuntimeError(f"Topaz upscale {status}: {message}")
            time.sleep(interval)
            interval = min(interval * 1.5, MAX_POLL_SECONDS)
        raise TimeoutError(
            f"Topaz request {request_id} did not finish within {MAX_WAIT_SECONDS // 60} minutes. "
            "It may still complete; re-check via the request id."
        )

    def _save_video(self, request_id: str, download_url: str) -> VideoUrlArtifact:
        """Persist the video locally: Topaz's download URL expires (24 h TTL)."""
        filename = f"topaz_{request_id[:8]}.mp4"
        response = requests.get(download_url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        if not response.ok:
            raise RuntimeError(f"Could not download the upscaled video (HTTP {response.status_code}).")
        try:
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(response.content, filename)
        except Exception:
            logger.warning("Could not save %s to static files; using the Topaz URL", filename, exc_info=True)
            saved_url = download_url
        self._save_copy_to_output_directory(response.content, request_id)
        return VideoUrlArtifact(value=saved_url, name=filename)

    @staticmethod
    def _guess_container(data: bytes) -> str:
        if data[:4] == b"\x1a\x45\xdf\xa3":
            return "mkv"
        if data[4:8] == b"ftyp" and data[8:10] == b"qt":
            return "mov"
        return "mp4"

    # -- Output directory + media input handling (copied from
    # hyperreal/heygen/avatar_video.py; node files are self-contained) ------

    def _save_copy_to_output_directory(self, data: bytes, request_id: str) -> None:
        """Optionally write the video into the user-chosen folder; failures are reported, not fatal."""
        directory = (self.parameter_values.get("output_directory") or "").strip()
        if not directory:
            return
        try:
            dir_path = self._resolve_directory(directory)
            dir_path.mkdir(parents=True, exist_ok=True)
            source = self.parameter_values.get("video")
            source_name = Path(str(getattr(source, "name", "") or "")).stem
            base = re.sub(r"[^A-Za-z0-9_-]+", "_", source_name).strip("_")
            base = f"{base}_upscaled" if base else f"topaz_{request_id[:8]}"
            target = dir_path / f"{base}.mp4"
            counter = 1
            while target.exists():
                target = dir_path / f"{base}_{counter}.mp4"
                counter += 1
            target.write_bytes(data)
            self._saved_files.append(str(target))
        except Exception as e:
            logger.warning("Could not save upscaled video to %r", directory, exc_info=True)
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
