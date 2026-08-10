from __future__ import annotations

import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from griptape.artifacts.image_url_artifact import ImageUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

API_BASE = "https://api.topazlabs.com"
API_KEY_NAME = "TOPAZ_API_KEY"
REQUEST_TIMEOUT_SECONDS = 120
DOWNLOAD_TIMEOUT_SECONDS = 300
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # Topaz request cap
MAX_INPUT_MEGAPIXELS = 512
MAX_OUTPUT_MEGAPIXELS = 1024
MAX_WAIT_SECONDS = 15 * 60
INITIAL_POLL_SECONDS = 2.0
MAX_POLL_SECONDS = 10.0

# /image/v1/enhance/async models. The generative family (Standard MAX, Recovery
# V2, Wonder, Redefine) lives behind /enhance-gen/async and is deliberately not
# offered here: it invents detail freely, which is wrong for a likeness.
MODELS = [
    "High Fidelity V2",
    "Standard V2",
    "Low Resolution V2",
    "CGI",
    "Text Refine",
]
# Title case, confirmed by the API itself: a lowercase value is rejected with
# 'must be one of [All Foreground Background]'. The published docs show both
# casings in different places, so trust the server, and normalise on the way out
# so a workflow saved with the old lowercase value keeps working.
SUBJECT_DETECTION = ["All", "Foreground", "Background"]
SUBJECT_LOOKUP = {value.lower(): value for value in SUBJECT_DETECTION}
# Lowercase here, per the docs — and note webp is NOT accepted on output.
OUTPUT_FORMATS = ["png", "jpeg", "jpg", "tiff", "tif"]
TERMINAL_FAILURE_STATUSES = {"failed", "cancelled", "canceled", "error"}
# Float knobs use -1 to mean "omit the field and let Topaz auto-tune", which is
# not the same as sending 0 (an explicit "none").
AUTO = -1.0


class TopazImageUpscale(SuccessFailureNode):
    """Upscale and restore a still image via the Topaz Labs Image API.

    Built for the zoomed-face lipsync pass: a head crop taken out of a full-body
    frame has too few pixels across the eyes and mouth, and the lipsync model
    then produces eyelid artifacting on blinks. Enlarging the crop with real
    detail before generation gives the model something to work from.

    Contract verified against developer.topazlabs.com on 2026-08-08: async only
    (POST /image/v1/enhance/async -> GET /image/v1/status/{id} -> GET
    /image/v1/download/{id}), multipart upload, billed per output megapixel.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "image/topaz",
            "description": "Upscale and restore a still image with Topaz Labs, with face recovery for "
            "lipsync-quality head crops.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="image",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageArtifact",
                tooltip="The image to upscale — e.g. the zoomed_image output of Zoom To Head.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="model",
                type="str",
                default_value="High Fidelity V2",
                tooltip="High Fidelity V2 suits clean, already-sharp sources such as an AI-generated frame. "
                "Use Low Resolution V2 for genuinely soft or small originals, Standard V2 as a general "
                "fallback, CGI for rendered art.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=MODELS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_long_edge",
                input_types=["int"],
                type="int",
                default_value=1920,
                tooltip="Target size of the image's longest side, in pixels. Only this one dimension is sent "
                "to Topaz so the other scales proportionally and the aspect ratio is preserved exactly — "
                "asking for both would letterbox. 0 = let the model pick its own scale.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="face_enhancement",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip="Run Topaz's face recovery. This is the setting that matters for a lipsync source — "
                "it rebuilds eyes, eyelids and mouth detail.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="face_enhancement_strength",
                input_types=["float"],
                type="float",
                default_value=0.8,
                tooltip="How hard face recovery is applied, 0-1.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="face_enhancement_creativity",
                input_types=["float"],
                type="float",
                default_value=0.0,
                tooltip="0 = reconstruct only what is there; higher lets Topaz invent facial detail. Keep "
                "this at 0 when the subject is a real, recognisable person — creativity drifts the likeness, "
                "which is exactly what you do not want feeding a lipsync of someone specific.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="subject_detection",
                type="str",
                default_value="All",
                tooltip="Which part of the frame the model works on.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=SUBJECT_DETECTION)},
            )
        )
        for knob, blurb in (
            ("denoise", "Noise reduction"),
            ("sharpen", "Sharpening"),
            ("fix_compression", "Compression-artifact removal"),
            ("strength", "Overall model strength"),
        ):
            self.add_parameter(
                Parameter(
                    name=knob,
                    input_types=["float"],
                    type="float",
                    default_value=AUTO,
                    tooltip=f"{blurb}, 0-1. Leave at -1 to omit the field entirely and let Topaz auto-tune "
                    "(not the same as 0, which explicitly asks for none).",
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                )
            )
        self.add_parameter(
            Parameter(
                name="output_format",
                type="str",
                default_value="png",
                tooltip="png is lossless and the right choice when the result feeds another generative pass. "
                "Topaz does not accept webp on output.",
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
                tooltip="Optional folder to also save the result into, e.g. {project_dir}/outputs.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="upscaled_image",
                output_type="ImageUrlArtifact",
                tooltip="The upscaled image — feed this to the lipsync node.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="process_id",
                output_type="str",
                tooltip="Topaz process id, for chasing a job that timed out on our side.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="size_report",
                output_type="json",
                tooltip="Source and result dimensions plus the achieved scale factor.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the upscale result",
            result_details_placeholder="Upscale details will appear here.",
        )

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

            data = self._artifact_to_bytes(self.parameter_values.get("image"), "image")
            source_w, source_h = self._decode_size(data)
            self._check_limits(data, source_w, source_h)

            fields = self._build_fields(source_w, source_h)
            process_id = self._submit(api_key, data, fields)
            self.parameter_output_values["process_id"] = process_id
            logger.info("Topaz image request %s submitted (%dx%d source)", process_id, source_w, source_h)

            self._poll(api_key, process_id)
            result = self._download(api_key, process_id)

            out_w, out_h = self._decode_size(result)
            scale = (max(out_w, out_h) / max(source_w, source_h)) if max(source_w, source_h) else 0.0
            self.parameter_output_values["size_report"] = {
                "schema": "hyperreal.topaz_image/1",
                "source": {"width": source_w, "height": source_h},
                "output": {"width": out_w, "height": out_h},
                "scale": round(scale, 4),
                "model": fields.get("model"),
                "face_enhancement": fields.get("face_enhancement"),
            }
            self.parameter_output_values["upscaled_image"] = self._publish(result, process_id)

            elapsed = int(time.monotonic() - started)
            megapixels = out_w * out_h / 1_000_000
            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"{source_w}x{source_h} -> {out_w}x{out_h} ({scale:.2f}x) with {fields.get('model')} "
                    f"in {elapsed}s; billed on {megapixels:.1f} output MP."
                    f"{self._aspect_note(source_w, source_h, out_w, out_h)}{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    # -- Request building ----------------------------------------------------

    def _build_fields(self, source_w: int, source_h: int) -> dict[str, str]:
        """Multipart form fields. Everything must be a string; -1 floats are omitted."""
        output_format = str(self.parameter_values.get("output_format") or "png").strip().lower()
        fields: dict[str, str] = {
            "model": self.parameter_values.get("model") or "High Fidelity V2",
            "output_format": output_format if output_format in OUTPUT_FORMATS else "png",
        }

        long_edge = int(self.parameter_values.get("output_long_edge") or 0)
        if long_edge > 0:
            # Send exactly one dimension. Topaz scales the other proportionally, so the
            # aspect ratio survives untouched; sending both would letterbox whenever the
            # requested shape differs even slightly from the source's.
            key = "output_width" if source_w >= source_h else "output_height"
            fields[key] = str(long_edge)

        face_on = bool(self.parameter_values.get("face_enhancement", True))
        fields["face_enhancement"] = "true" if face_on else "false"
        if face_on:
            fields["face_enhancement_strength"] = str(
                self._clamp01(self.parameter_values.get("face_enhancement_strength"), 0.8)
            )
            fields["face_enhancement_creativity"] = str(
                self._clamp01(self.parameter_values.get("face_enhancement_creativity"), 0.0)
            )

        subject = str(self.parameter_values.get("subject_detection") or "All").strip().lower()
        fields["subject_detection"] = SUBJECT_LOOKUP.get(subject, "All")

        for knob in ("denoise", "sharpen", "fix_compression", "strength"):
            raw = self.parameter_values.get(knob)
            if raw is None:
                continue
            value = float(raw)
            if value < 0:
                continue  # AUTO: omit so Topaz picks
            fields[knob] = str(min(1.0, max(0.01 if knob == "strength" else 0.0, value)))
        return fields

    @staticmethod
    def _clamp01(raw: Any, fallback: float) -> float:
        try:
            return min(1.0, max(0.0, float(raw)))
        except (TypeError, ValueError):
            return fallback

    def _check_limits(self, data: bytes, width: int, height: int) -> None:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"The image is {len(data) / (1024 * 1024):.0f} MB, over Topaz's 500 MB request limit."
            )
        source_mp = width * height / 1_000_000
        if source_mp > MAX_INPUT_MEGAPIXELS:
            raise ValueError(
                f"The image is {source_mp:.0f} MP, over Topaz's {MAX_INPUT_MEGAPIXELS} MP input limit."
            )
        long_edge = int(self.parameter_values.get("output_long_edge") or 0)
        if long_edge > 0 and max(width, height):
            scale = long_edge / max(width, height)
            output_mp = source_mp * scale * scale
            if output_mp > MAX_OUTPUT_MEGAPIXELS:
                raise ValueError(
                    f"output_long_edge {long_edge} would produce {output_mp:.0f} MP, over Topaz's "
                    f"{MAX_OUTPUT_MEGAPIXELS} MP output limit."
                )
            if scale < 1.0:
                logger.warning(
                    "output_long_edge %d is smaller than the source's %d — this will downscale, not upscale.",
                    long_edge, max(width, height),
                )

    @staticmethod
    def _aspect_note(sw: int, sh: int, ow: int, oh: int) -> str:
        """Letterboxing would show up as a changed aspect ratio; say so rather than pass it on silently."""
        if not (sw and sh and ow and oh):
            return ""
        if abs((ow / oh) - (sw / sh)) > 0.01:
            return (
                f"\nWARNING: the aspect ratio changed ({sw / sh:.4f} -> {ow / oh:.4f}). Topaz may have "
                "letterboxed the result, which would corrupt a downstream lipsync pass. Check the image."
            )
        return ""

    # -- Topaz API -----------------------------------------------------------

    def _submit(self, api_key: str, data: bytes, fields: dict[str, str]) -> str:
        filename, mime = self._source_naming(data)
        response = None
        for _ in range(4):
            response = requests.post(
                f"{API_BASE}/image/v1/enhance/async",
                headers={"X-API-Key": api_key},
                files={"image": (filename, data, mime)},
                data=fields,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code != 429:
                break
            self._sleep_for_retry(response)
        assert response is not None
        self._raise_for_api_error(response, "job submission")
        payload = response.json()
        process_id = payload.get("process_id") or payload.get("processId") or payload.get("id")
        if not process_id:
            raise RuntimeError(f"Topaz accepted the image but returned no process_id: {response.text[:300]}")
        return str(process_id)

    def _poll(self, api_key: str, process_id: str) -> dict:
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        interval = INITIAL_POLL_SECONDS
        while time.monotonic() < deadline:
            response = self._get(f"/image/v1/status/{process_id}", api_key)
            self._raise_for_api_error(response, "status polling")
            detail = response.json() if response.content else {}
            status = str(detail.get("status") or "").lower()
            if status in ("completed", "complete", "success", "succeeded"):
                return detail
            if status in TERMINAL_FAILURE_STATUSES:
                message = detail.get("message") or detail.get("error") or "No failure message provided."
                raise RuntimeError(f"Topaz image upscale {status}: {message}")
            time.sleep(interval)
            interval = min(interval * 1.5, MAX_POLL_SECONDS)
        raise TimeoutError(
            f"Topaz process {process_id} did not finish within {MAX_WAIT_SECONDS // 60} minutes. "
            "It may still complete — the process_id output can be used to fetch it."
        )

    def _download(self, api_key: str, process_id: str) -> bytes:
        response = self._get(f"/image/v1/download/{process_id}", api_key, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        self._raise_for_api_error(response, "download")
        # Documented to return {"url": ...}, but tolerate a direct byte body so a
        # contract change does not break the node outright.
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "json" in content_type:
            payload = response.json() or {}
            # Documented keys are download_url / head_url / expiry. head_url is
            # deliberately excluded: it is presigned for metadata (HEAD), and an S3
            # signature is bound to its method, so a GET against it would 403.
            url = next(
                (payload[key] for key in ("download_url", "url", "get_url", "output_url") if payload.get(key)),
                None,
            )
            if not url:
                raise RuntimeError(
                    f"Topaz returned no recognised download URL for {process_id}. "
                    f"Keys present: {sorted(payload)}."
                )
            fetched = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
            if not fetched.ok:
                raise RuntimeError(
                    f"Could not download the upscaled image (HTTP {fetched.status_code}). The presigned "
                    "link expires one hour after the job completes."
                )
            return fetched.content
        if not response.content:
            raise RuntimeError(f"Topaz returned an empty download body for {process_id}.")
        return response.content

    def _get(self, path: str, api_key: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> requests.Response:
        response = None
        for _ in range(4):
            response = requests.get(f"{API_BASE}{path}", headers={"X-API-Key": api_key}, timeout=timeout)
            if response.status_code != 429:
                break
            self._sleep_for_retry(response)
        assert response is not None
        return response

    @staticmethod
    def _sleep_for_retry(response: requests.Response) -> None:
        try:
            retry_after = float(response.headers.get("Retry-After", "5"))
        except ValueError:
            retry_after = 5.0
        logger.warning("Topaz rate limit hit; retrying in %.0fs", retry_after)
        time.sleep(min(retry_after, 60.0))

    def _raise_for_api_error(self, response: requests.Response, context: str) -> None:
        if response.ok:
            return
        if response.status_code == 401:
            raise RuntimeError(f"Topaz rejected the API key ({context}). Check {API_KEY_NAME} in your settings.")
        message = ""
        try:
            body = response.json()
            message = body.get("message") or body.get("error") or body.get("detail") or ""
            if isinstance(message, dict):
                message = message.get("message") or str(message)
        except Exception:
            message = response.text[:300]
        raise RuntimeError(f"Topaz API error during {context} (HTTP {response.status_code}): {message}")

    # -- Image helpers -------------------------------------------------------

    @staticmethod
    def _decode_size(data: bytes) -> tuple[int, int]:
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError("Could not decode the image.")
        return int(image.shape[1]), int(image.shape[0])

    @staticmethod
    def _source_naming(data: bytes) -> tuple[str, str]:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "source.png", "image/png"
        if data[:2] == b"\xff\xd8":
            return "source.jpg", "image/jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "source.webp", "image/webp"
        return "source.png", "image/png"

    # -- Output --------------------------------------------------------------

    def _publish(self, data: bytes, process_id: str) -> ImageUrlArtifact:
        extension = str(self.parameter_values.get("output_format") or "png").lower()
        extension = {"jpeg": "jpg", "tiff": "tif"}.get(extension, extension)
        filename = f"topaz_image_{process_id[:8] if process_id else uuid.uuid4().hex[:8]}.{extension}"
        try:
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
        except Exception:
            logger.warning("Could not save %s to static files", filename, exc_info=True)
            raise
        self._save_copy_to_output_directory(data, filename)
        return ImageUrlArtifact(value=saved_url, name=filename)

    def _save_copy_to_output_directory(self, data: bytes, filename: str) -> None:
        """Optionally write the result into the user-chosen folder; failures are reported, not fatal."""
        directory = (self.parameter_values.get("output_directory") or "").strip()
        if not directory:
            return
        try:
            dir_path = self._resolve_directory(directory)
            dir_path.mkdir(parents=True, exist_ok=True)
            base, _, ext = filename.rpartition(".")
            target = dir_path / filename
            counter = 1
            while target.exists():
                target = dir_path / f"{base}_{counter}.{ext}"
                counter += 1
            target.write_bytes(data)
            self._saved_files.append(str(target))
        except Exception as e:
            logger.warning("Could not save the upscaled image to %r", directory, exc_info=True)
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

    # -- Media input handling (copied from hyperreal/heygen/avatar_video.py;
    # node files are self-contained by library convention) ------------------

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
