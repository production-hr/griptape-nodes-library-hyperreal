from __future__ import annotations

import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from griptape.artifacts.image_url_artifact import ImageUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

API_BASE = "https://api.wavespeed.ai/api/v3"
API_KEY_NAME = "WAVESPEED_API_KEY"
REQUEST_TIMEOUT_SECONDS = 60
DOWNLOAD_TIMEOUT_SECONDS = 300
UPLOAD_TIMEOUT_SECONDS = 900
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # WaveSpeed media upload cap
MAX_WAIT_SECONDS = 10 * 60
INITIAL_POLL_SECONDS = 2.0
MAX_POLL_SECONDS = 5.0

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

# Model path -> max reference images (nano-banana caps at 14, gpt-image-2 at 16).
MODELS = {
    "google/nano-banana-pro/edit": 14,
    "google/nano-banana-2/edit": 14,
    "openai/gpt-image-2/edit": 16,
}
# Ratios accepted by all three models; "auto" omits the field (model auto-detects).
ASPECT_RATIOS = ["auto", "1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
RESOLUTIONS = ["1k", "2k", "4k"]
QUALITIES = ["low", "medium", "high"]
OUTPUT_FORMATS = ["png", "jpeg"]
TERMINAL_FAILURE_STATUSES = {"failed", "cancelled", "canceled", "timeout"}

_EXTENSION_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "video/mp4": "mp4",
}


def _sniff_mime(data: bytes, fallback: str) -> str:
    """Detect the content type from magic bytes; artifact metadata is unreliable across sources."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    return fallback


class WaveSpeedImageEdit(SuccessFailureNode):
    """Edit/generate an image from a prompt plus reference images via WaveSpeed.

    One node covers the three edit models (Nano Banana 2/Pro, GPT Image 2)
    because they share the same input signature; the model is a dropdown.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "image/wavespeed",
            "description": "Edit an image from a prompt + reference images (Nano Banana 2/Pro, GPT Image 2) via WaveSpeed.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="prompt",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Text instruction describing the desired edit.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            ParameterList(
                name="images",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                default_value=[],
                tooltip="Reference images (up to 14 for the Google models, 16 for GPT Image 2).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="model",
                type="str",
                default_value="google/nano-banana-pro/edit",
                tooltip="Which WaveSpeed edit model to use.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=list(MODELS.keys()))},
            )
        )
        self.add_parameter(
            Parameter(
                name="aspect_ratio",
                type="str",
                default_value="auto",
                tooltip="Output aspect ratio. 'auto' lets the model decide from the references.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=ASPECT_RATIOS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="resolution",
                type="str",
                default_value="1k",
                tooltip="Output resolution tier. Higher tiers cost more.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=RESOLUTIONS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="quality",
                type="str",
                default_value="medium",
                tooltip="Quality tier — only applies to openai/gpt-image-2/edit (ignored by the Google models).",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=QUALITIES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_format",
                type="str",
                default_value="png",
                tooltip="Output image format.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=OUTPUT_FORMATS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the edited image into, e.g. {project_dir}/outputs. "
                "Leave empty to skip saving a file copy.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="image",
                output_type="ImageUrlArtifact",
                tooltip="The edited image.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="prediction_id",
                output_type="str",
                tooltip="WaveSpeed prediction id, for support and debugging.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the WaveSpeed image edit result",
            result_details_placeholder="Edit details will appear here.",
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

            prompt = (self.parameter_values.get("prompt") or "").strip()
            if not prompt:
                raise ValueError("No prompt set.")

            model = self.parameter_values.get("model") or "google/nano-banana-pro/edit"
            references = [a for a in self.get_parameter_list_value("images") if a is not None]
            if not references:
                raise ValueError("Connect at least one reference image.")
            max_images = MODELS.get(model, 14)
            if len(references) > max_images:
                raise ValueError(f"{model} accepts at most {max_images} reference images (got {len(references)}).")

            image_urls = [
                self._artifact_to_url(api_key, artifact, f"image {i + 1}") for i, artifact in enumerate(references)
            ]

            body: dict[str, Any] = {
                "prompt": prompt,
                "images": image_urls,
                "resolution": self.parameter_values.get("resolution") or "1k",
                "output_format": self.parameter_values.get("output_format") or "png",
            }
            aspect_ratio = self.parameter_values.get("aspect_ratio") or "auto"
            if aspect_ratio != "auto":
                body["aspect_ratio"] = aspect_ratio
            if model.startswith("openai/"):
                body["quality"] = self.parameter_values.get("quality") or "medium"

            prediction_id = self._submit_prediction(api_key, model, body)
            self.parameter_output_values["prediction_id"] = prediction_id
            logger.info("WaveSpeed prediction %s submitted to %s; polling", prediction_id, model)

            outputs = self._poll_prediction(api_key, prediction_id)
            if not outputs:
                raise RuntimeError(f"WaveSpeed prediction {prediction_id} completed but returned no outputs.")

            artifact = self._save_image(prediction_id, outputs[0])
            self.parameter_output_values["image"] = artifact

            elapsed = int(time.monotonic() - started)
            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=f"Prediction {prediction_id} ({model}) completed in {elapsed}s.{saved_note}",
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    # -- WaveSpeed API helpers ----------------------------------------------

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def _raise_for_api_error(self, response: requests.Response, context: str) -> None:
        if response.ok:
            return
        if response.status_code == 401:
            raise RuntimeError(f"WaveSpeed rejected the API key ({context}). Check {API_KEY_NAME} in your settings.")
        message = ""
        try:
            body = response.json()
            message = body.get("message") or body.get("error") or ""
        except Exception:
            message = response.text[:300]
        raise RuntimeError(f"WaveSpeed API error during {context} (HTTP {response.status_code}): {message}")

    def _upload_media(self, api_key: str, data: bytes, label: str) -> str:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"The {label} file is {len(data) / (1024 * 1024):.0f} MB, over WaveSpeed's 200 MB upload limit."
            )
        mime = _sniff_mime(data, "application/octet-stream")
        filename = f"upload_{uuid.uuid4().hex[:8]}.{_EXTENSION_BY_MIME.get(mime, 'bin')}"
        response = requests.post(
            f"{API_BASE}/media/upload/binary",
            headers=self._headers(api_key),
            files={"file": (filename, data, mime)},
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        self._raise_for_api_error(response, f"{label} upload")
        # Live API returns data.download_url (docs say data.url) — accept both.
        payload = response.json().get("data") or {}
        url = payload.get("download_url") or payload.get("url") or ""
        if not url:
            raise RuntimeError(f"WaveSpeed {label} upload returned no URL: {response.text[:300]}")
        return url

    def _artifact_to_url(self, api_key: str, artifact: Any, label: str) -> str:
        """Public http(s) URLs pass through; everything else is uploaded to WaveSpeed."""
        value = getattr(artifact, "value", artifact)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            host = (urlparse(value).hostname or "").lower()
            if host not in LOCAL_HOSTS:
                return value
        return self._upload_media(api_key, self._artifact_to_bytes(artifact, label), label)

    def _submit_prediction(self, api_key: str, model: str, body: dict) -> str:
        response = requests.post(
            f"{API_BASE}/{model}", headers=self._headers(api_key), json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
        self._raise_for_api_error(response, "prediction submission")
        payload = response.json().get("data") or {}
        prediction_id = payload.get("id")
        if not prediction_id:
            raise RuntimeError(f"WaveSpeed submission returned no prediction id: {response.text[:300]}")
        return prediction_id

    def _poll_prediction(self, api_key: str, prediction_id: str) -> list[str]:
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        interval = INITIAL_POLL_SECONDS
        while time.monotonic() < deadline:
            response = requests.get(
                f"{API_BASE}/predictions/{prediction_id}/result",
                headers=self._headers(api_key),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            self._raise_for_api_error(response, "status polling")
            detail = response.json().get("data") or {}
            status = (detail.get("status") or "").lower()
            if status == "completed":
                return [url for url in (detail.get("outputs") or []) if url]
            if status in TERMINAL_FAILURE_STATUSES:
                error = detail.get("error") or "No failure message provided."
                raise RuntimeError(f"WaveSpeed prediction {status}: {error}")
            time.sleep(interval)
            interval = min(interval * 1.5, MAX_POLL_SECONDS)
        raise TimeoutError(
            f"WaveSpeed prediction {prediction_id} did not finish within {MAX_WAIT_SECONDS // 60} minutes."
        )

    def _save_image(self, prediction_id: str, url: str) -> ImageUrlArtifact:
        """Persist the image locally: WaveSpeed files are deleted after 7 days."""
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        if not response.ok:
            raise RuntimeError(f"Could not download the edited image (HTTP {response.status_code}).")
        ext = _EXTENSION_BY_MIME.get(_sniff_mime(response.content, "image/png"), "png")
        filename = f"wavespeed_{prediction_id[:8]}.{ext}"
        try:
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(response.content, filename)
        except Exception:
            logger.warning("Could not save %s to static files; using the WaveSpeed URL", filename, exc_info=True)
            saved_url = url
        self._save_copy_to_output_directory(response.content, prediction_id, ext)
        return ImageUrlArtifact(value=saved_url, name=filename)

    # -- Output directory + media input handling (copied from
    # hyperreal/heygen/avatar_video.py; node files are self-contained) ------

    def _save_copy_to_output_directory(self, data: bytes, prediction_id: str, ext: str) -> None:
        """Optionally write the result into the user-chosen folder; failures are reported, not fatal."""
        directory = (self.parameter_values.get("output_directory") or "").strip()
        if not directory:
            return
        try:
            dir_path = self._resolve_directory(directory)
            dir_path.mkdir(parents=True, exist_ok=True)
            base = f"wavespeed_{prediction_id[:8]}"
            target = dir_path / f"{base}.{ext}"
            counter = 1
            while target.exists():
                target = dir_path / f"{base}_{counter}.{ext}"
                counter += 1
            target.write_bytes(data)
            self._saved_files.append(str(target))
        except Exception as e:
            logger.warning("Could not save result to %r", directory, exc_info=True)
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
