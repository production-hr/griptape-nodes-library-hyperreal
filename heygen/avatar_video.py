from __future__ import annotations

import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

API_BASE = "https://api.heygen.com"
API_KEY_NAME = "HEYGEN_API_KEY"
REQUEST_TIMEOUT_SECONDS = 60
DOWNLOAD_TIMEOUT_SECONDS = 300
MAX_UPLOAD_BYTES = 32 * 1024 * 1024  # HeyGen /v3/assets limit
MAX_WAIT_SECONDS = 30 * 60
INITIAL_POLL_SECONDS = 5.0
MAX_POLL_SECONDS = 15.0

ASPECT_RATIOS = ["auto", "16:9", "9:16", "4:5", "5:4", "1:1"]
EXPRESSIVENESS_LEVELS = ["low", "medium", "high"]

_EXTENSION_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
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
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    return fallback


class HeyGenAvatarVideo(SuccessFailureNode):
    """Image + audio -> lipsync video via HeyGen's Avatar IV engine (the v3 default).

    Avatar IV is the only HeyGen engine that accepts arbitrary image input, so no
    engine parameter is exposed; omitting `engine` selects Avatar IV server-side.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/heygen",
            "description": "Generate a lipsync avatar video from an image and an audio file using HeyGen Avatar IV.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="image",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageArtifact",
                tooltip="The approved image to animate (png or jpeg, up to 32 MB).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="audio",
                input_types=["AudioArtifact", "AudioUrlArtifact"],
                type="AudioUrlArtifact",
                tooltip="The approved audio to lipsync to (mp3 or wav, up to 32 MB).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="video_title",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional title shown in the HeyGen dashboard.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="aspect_ratio",
                type="str",
                default_value="auto",
                tooltip="Output aspect ratio. 'auto' follows the input image.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=ASPECT_RATIOS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="motion_prompt",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional natural-language direction for body motion and gestures.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="expressiveness",
                type="str",
                default_value="low",
                tooltip="Energy and movement range of the avatar.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=EXPRESSIVENESS_LEVELS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="video",
                output_type="VideoUrlArtifact",
                tooltip="The generated lipsync video.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="video_id",
                output_type="str",
                tooltip="HeyGen video id, usable for translation and debugging.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the HeyGen video generation result",
            result_details_placeholder="Generation details will appear here.",
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
        started = time.monotonic()
        try:
            api_key = GriptapeNodes.SecretsManager().get_secret(API_KEY_NAME)
            if not api_key:
                raise ValueError(f"{API_KEY_NAME} is not set.")

            image_bytes = self._artifact_to_bytes(self.parameter_values.get("image"), "image")
            audio_bytes = self._artifact_to_bytes(self.parameter_values.get("audio"), "audio")

            image_asset_id = self._upload_asset(api_key, image_bytes, "image/jpeg", "image")
            audio_asset_id = self._upload_asset(api_key, audio_bytes, "audio/mpeg", "audio")

            video_id = self._submit_video(api_key, image_asset_id, audio_asset_id)
            self.parameter_output_values["video_id"] = video_id
            logger.info("HeyGen video %s submitted; polling for completion", video_id)

            detail = self._poll_video(api_key, video_id)
            video_url = detail.get("video_url")
            if not video_url:
                raise RuntimeError(f"HeyGen reported video {video_id} completed but returned no video_url.")

            artifact = self._save_video(video_id, video_url)
            self.parameter_output_values["video"] = artifact

            elapsed = int(time.monotonic() - started)
            duration = detail.get("duration")
            duration_note = f", duration {duration:.1f}s" if isinstance(duration, (int, float)) else ""
            self._set_status_results(
                was_successful=True,
                result_details=f"Video {video_id} generated in {elapsed}s{duration_note}.",
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    # -- HeyGen API helpers -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        json_body: dict | None = None,
        files: dict | None = None,
        extra_headers: dict | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> requests.Response:
        headers = {"X-Api-Key": api_key}
        if extra_headers:
            headers.update(extra_headers)
        response = None
        for _ in range(4):
            response = requests.request(
                method, f"{API_BASE}{path}", json=json_body, files=files, headers=headers, timeout=timeout
            )
            if response.status_code != 429:
                break
            try:
                retry_after = float(response.headers.get("Retry-After", "5"))
            except ValueError:
                retry_after = 5.0
            logger.warning("HeyGen rate limit hit on %s; retrying in %.0fs", path, retry_after)
            time.sleep(min(retry_after, 60.0))
        assert response is not None
        return response

    def _raise_for_api_error(self, response: requests.Response, context: str) -> None:
        if response.ok:
            return
        if response.status_code == 401:
            raise RuntimeError(f"HeyGen rejected the API key ({context}). Check {API_KEY_NAME} in your settings.")
        message = ""
        try:
            body = response.json()
            error = body.get("error") if isinstance(body.get("error"), dict) else body
            message = error.get("message") or error.get("msg") or ""
        except Exception:
            message = response.text[:300]
        raise RuntimeError(f"HeyGen API error during {context} (HTTP {response.status_code}): {message}")

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
        """Saved workflows store static files as workspace-relative paths rather than URLs."""
        path = Path(value)
        if path.is_absolute():
            return path if path.is_file() else None
        try:
            workspace = GriptapeNodes.ConfigManager().workspace_path
        except Exception:
            return None
        candidate = Path(workspace) / path
        return candidate if candidate.is_file() else None

    def _upload_asset(self, api_key: str, data: bytes, fallback_mime: str, label: str) -> str:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"The {label} file is {len(data) / (1024 * 1024):.1f} MB, over HeyGen's 32 MB upload limit."
            )
        mime = _sniff_mime(data, fallback_mime)
        filename = f"{label}.{_EXTENSION_BY_MIME.get(mime, 'bin')}"
        response = self._request(
            "POST",
            "/v3/assets",
            api_key,
            files={"file": (filename, data, mime)},
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self._raise_for_api_error(response, f"{label} upload")
        payload = response.json().get("data") or {}
        asset_id = payload.get("asset_id")
        if not asset_id:
            raise RuntimeError(f"HeyGen {label} upload returned no asset_id: {response.text[:300]}")
        return asset_id

    def _submit_video(self, api_key: str, image_asset_id: str, audio_asset_id: str) -> str:
        body: dict[str, Any] = {
            "type": "image",
            "image": {"type": "asset_id", "asset_id": image_asset_id},
            "audio_asset_id": audio_asset_id,
            "expressiveness": self.parameter_values.get("expressiveness") or "low",
        }
        aspect_ratio = self.parameter_values.get("aspect_ratio") or "auto"
        if aspect_ratio != "auto":
            body["aspect_ratio"] = aspect_ratio
        title = (self.parameter_values.get("video_title") or "").strip()
        if title:
            body["title"] = title
        motion_prompt = (self.parameter_values.get("motion_prompt") or "").strip()
        if motion_prompt:
            body["motion_prompt"] = motion_prompt

        response = self._request(
            "POST",
            "/v3/videos",
            api_key,
            json_body=body,
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self._raise_for_api_error(response, "video submission")
        payload = response.json().get("data") or {}
        video_id = payload.get("video_id")
        if not video_id:
            raise RuntimeError(f"HeyGen video submission returned no video_id: {response.text[:300]}")
        return video_id

    def _poll_video(self, api_key: str, video_id: str) -> dict:
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        interval = INITIAL_POLL_SECONDS
        while time.monotonic() < deadline:
            response = self._request("GET", f"/v3/videos/{video_id}", api_key)
            self._raise_for_api_error(response, "status polling")
            body = response.json()
            detail = body.get("data") or body
            status = detail.get("status")
            if status == "completed":
                return detail
            if status == "failed":
                code = detail.get("failure_code") or "unknown"
                message = detail.get("failure_message") or "No failure message provided."
                raise RuntimeError(f"HeyGen video generation failed [{code}]: {message}")
            time.sleep(interval)
            interval = min(interval * 1.5, MAX_POLL_SECONDS)
        raise TimeoutError(
            f"HeyGen video {video_id} did not finish within {MAX_WAIT_SECONDS // 60} minutes. "
            "It may still complete; check the HeyGen dashboard."
        )

    def _save_video(self, video_id: str, video_url: str) -> VideoUrlArtifact:
        """Persist the video locally: HeyGen's video_url is presigned and expires."""
        filename = f"heygen_{video_id}.mp4"
        response = requests.get(video_url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        if not response.ok:
            raise RuntimeError(f"Could not download the generated video (HTTP {response.status_code}).")
        try:
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(response.content, filename)
        except Exception:
            logger.warning("Could not save %s to static files; using the presigned URL", filename, exc_info=True)
            saved_url = video_url
        return VideoUrlArtifact(value=saved_url, name=filename)
