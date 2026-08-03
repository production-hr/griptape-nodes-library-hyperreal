from __future__ import annotations

import difflib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

API_BASE = "https://api.heygen.com"
API_KEY_NAME = "HEYGEN_API_KEY"
REQUEST_TIMEOUT_SECONDS = 60
DOWNLOAD_TIMEOUT_SECONDS = 300
MAX_UPLOAD_BYTES = 32 * 1024 * 1024  # HeyGen /v3/assets limit
MAX_WAIT_SECONDS = 45 * 60
INITIAL_POLL_SECONDS = 5.0
MAX_POLL_SECONDS = 15.0

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

TRANSLATE_MODES = ["speed", "precision"]


class HeyGenVideoTranslate(SuccessFailureNode):
    """Translate a video into one or more languages with lip-sync via HeyGen.

    All languages are submitted in a single API call (one translation id per
    language) and polled individually, so one failing language never discards
    the other results.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/heygen",
            "description": "Translate a video into one or more languages with lip-sync and voice preservation.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The source video to translate (e.g. the HeyGen Avatar Video output).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            ParameterList(
                name="target_languages",
                input_types=["str", "list[str]"],
                default_value=[],
                tooltip='Target languages in HeyGen format, e.g. "Spanish (Spain)", "Catalan (Spain)".',
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="title",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional title prefix shown in the HeyGen dashboard.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="mode",
                type="str",
                default_value="speed",
                tooltip="'speed' for fast turnaround, 'precision' for higher lip-sync quality.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=TRANSLATE_MODES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="videos",
                output_type="list[VideoUrlArtifact]",
                tooltip="Translated videos, in the same order as the successful target languages.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="language_map",
                output_type="json",
                tooltip="Mapping of language -> translated video URL, for downstream routing.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Per-language results of the translation run",
            result_details_placeholder="Translation details will appear here.",
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

            languages = [str(lang).strip() for lang in self.get_parameter_list_value("target_languages")]
            languages = [lang for lang in languages if lang]
            if not languages:
                raise ValueError('Add at least one target language, e.g. "Spanish (Spain)".')

            self._validate_languages(api_key, languages)
            video_ref = self._resolve_video_reference(api_key)
            ids_by_language = self._submit_translations(api_key, video_ref, languages)
            results = self._poll_translations(api_key, ids_by_language)

            succeeded = [lang for lang in languages if results[lang].get("artifact") is not None]
            failed = [lang for lang in languages if lang not in succeeded]

            self.parameter_output_values["videos"] = [results[lang]["artifact"] for lang in succeeded]
            self.parameter_output_values["language_map"] = {lang: results[lang]["artifact"].value for lang in succeeded}

            detail_lines = [f"{lang}: OK" for lang in succeeded]
            detail_lines += [f"{lang}: FAILED - {results[lang].get('error', 'unknown error')}" for lang in failed]
            details = "\n".join(detail_lines)

            if not succeeded:
                raise RuntimeError(f"All translations failed.\n{details}")
            # Partial failure still counts as success so surviving languages flow downstream.
            self._set_status_results(was_successful=True, result_details=details)
            if failed:
                logger.warning("HeyGen translation finished with failures:\n%s", details)
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

    def _validate_languages(self, api_key: str, languages: list[str]) -> None:
        """Fail at submit time with suggestions rather than mid-poll with an opaque error."""
        response = self._request("GET", "/v3/video-translations/languages", api_key)
        if not response.ok:
            logger.warning("Could not fetch HeyGen language list (HTTP %s); skipping validation", response.status_code)
            return
        body = response.json()
        payload = body.get("data") or body
        supported: list[str] = []
        if isinstance(payload, list):
            supported = [str(x) for x in payload]
        elif isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list) and all(isinstance(x, str) for x in value):
                    supported = value
                    break
        if not supported:
            logger.warning("Unrecognized HeyGen language list response; skipping validation")
            return

        by_lower = {lang.lower(): lang for lang in supported}
        problems = []
        for lang in languages:
            if lang.lower() not in by_lower:
                suggestions = difflib.get_close_matches(lang, supported, n=3, cutoff=0.4)
                hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                problems.append(f'"{lang}" is not a supported HeyGen output language.{hint}')
        if problems:
            raise ValueError(" ".join(problems))

    def _resolve_video_reference(self, api_key: str) -> dict[str, str]:
        """HeyGen must be able to fetch the source video, so local sources are re-uploaded as assets."""
        artifact = self.parameter_values.get("video")
        if artifact is None:
            raise ValueError("No video input connected.")
        value = getattr(artifact, "value", artifact)

        if isinstance(value, bytes):
            data = value
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            host = (urlparse(value).hostname or "").lower()
            if host not in LOCAL_HOSTS:
                return {"type": "url", "url": value}
            response = requests.get(value, timeout=DOWNLOAD_TIMEOUT_SECONDS)
            if not response.ok:
                raise RuntimeError(f"Could not read the local video at {value} (HTTP {response.status_code}).")
            data = response.content
        elif isinstance(value, str) and value:
            # Saved workflows store static files as workspace-relative paths rather than URLs.
            path = self._resolve_workspace_path(value)
            if path is None:
                raise ValueError(f"Unsupported video input of type {type(artifact).__name__} (value: {value[:120]!r}).")
            data = path.read_bytes()
        else:
            raise ValueError(f"Unsupported video input of type {type(artifact).__name__}.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"The source video is {len(data) / (1024 * 1024):.1f} MB, over HeyGen's 32 MB upload limit. "
                "Host it on a public URL instead of a local file."
            )
        upload = self._request(
            "POST",
            "/v3/assets",
            api_key,
            files={"file": ("video.mp4", data, "video/mp4")},
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self._raise_for_api_error(upload, "video upload")
        asset_id = (upload.json().get("data") or {}).get("asset_id")
        if not asset_id:
            raise RuntimeError(f"HeyGen video upload returned no asset_id: {upload.text[:300]}")
        return {"type": "asset_id", "asset_id": asset_id}

    def _resolve_workspace_path(self, value: str) -> Path | None:
        path = Path(value)
        if path.is_absolute():
            return path if path.is_file() else None
        try:
            workspace = GriptapeNodes.ConfigManager().workspace_path
        except Exception:
            return None
        candidate = Path(workspace) / path
        return candidate if candidate.is_file() else None

    def _submit_translations(self, api_key: str, video_ref: dict[str, str], languages: list[str]) -> dict[str, str]:
        body: dict[str, Any] = {
            "video": video_ref,
            "output_languages": languages,
            "mode": self.parameter_values.get("mode") or "speed",
        }
        title = (self.parameter_values.get("title") or "").strip()
        if title:
            body["title"] = title

        response = self._request(
            "POST",
            "/v3/video-translations",
            api_key,
            json_body=body,
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self._raise_for_api_error(response, "translation submission")
        ids = (response.json().get("data") or {}).get("video_translation_ids") or []
        if len(ids) != len(languages):
            raise RuntimeError(
                f"HeyGen returned {len(ids)} translation ids for {len(languages)} languages: {response.text[:300]}"
            )
        # The API returns one id per requested language, in request order.
        return dict(zip(languages, ids, strict=True))

    def _poll_translations(self, api_key: str, ids_by_language: dict[str, str]) -> dict[str, dict]:
        results: dict[str, dict] = {lang: {"id": job_id} for lang, job_id in ids_by_language.items()}
        pending = set(ids_by_language)
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        interval = INITIAL_POLL_SECONDS

        while pending and time.monotonic() < deadline:
            for lang in sorted(pending):
                job_id = results[lang]["id"]
                try:
                    response = self._request("GET", f"/v3/video-translations/{job_id}", api_key)
                    self._raise_for_api_error(response, f"status polling ({lang})")
                    detail = response.json().get("data") or response.json()
                except Exception as e:
                    results[lang]["error"] = str(e)
                    pending.discard(lang)
                    continue

                status = detail.get("status")
                if status == "completed":
                    try:
                        results[lang]["artifact"] = self._save_video(lang, job_id, detail.get("video_url"))
                    except Exception as e:
                        results[lang]["error"] = str(e)
                    pending.discard(lang)
                elif status == "failed":
                    results[lang]["error"] = detail.get("failure_message") or "No failure message provided."
                    pending.discard(lang)
            if pending:
                time.sleep(interval)
                interval = min(interval * 1.5, MAX_POLL_SECONDS)

        for lang in pending:
            results[lang]["error"] = f"Timed out after {MAX_WAIT_SECONDS // 60} minutes; check the HeyGen dashboard."
        return results

    def _save_video(self, language: str, job_id: str, video_url: str | None) -> VideoUrlArtifact:
        """Persist the video locally: HeyGen's video_url is presigned and expires."""
        if not video_url:
            raise RuntimeError("HeyGen reported the translation completed but returned no video_url.")
        safe_language = re.sub(r"[^A-Za-z0-9_-]+", "_", language).strip("_").lower()
        filename = f"heygen_translate_{safe_language}_{job_id[:8]}.mp4"
        response = requests.get(video_url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        if not response.ok:
            raise RuntimeError(f"Could not download the translated video (HTTP {response.status_code}).")
        try:
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(response.content, filename)
        except Exception:
            logger.warning("Could not save %s to static files; using the presigned URL", filename, exc_info=True)
            saved_url = video_url
        return VideoUrlArtifact(value=saved_url, name=filename)
