from __future__ import annotations

import base64
import logging
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import boto3
import requests
from botocore.exceptions import ClientError, EndpointConnectionError
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")

DOWNLOAD_TIMEOUT_SECONDS = 300
KEY_SECRET_NAME = "DO_SPACES_KEY"
SECRET_SECRET_NAME = "DO_SPACES_SECRET"
REGION_SECRET_NAME = "DO_SPACES_REGION"
ENDPOINT_SECRET_NAME = "DO_SPACES_ENDPOINT"

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


def _sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


class UploadToSpaces(SuccessFailureNode):
    """Upload a media artifact to a DigitalOcean Spaces bucket and return its public URL.

    Spaces is S3-compatible, so this uses boto3 with a custom endpoint_url. The public
    URL is what external APIs (like ViewComfy) fetch, which local static-file URLs
    can't provide.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "storage/spaces",
            "description": "Upload a media artifact to a DigitalOcean Spaces bucket and return its public URL.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="artifact",
                input_types=[
                    "ImageArtifact",
                    "ImageUrlArtifact",
                    "AudioArtifact",
                    "AudioUrlArtifact",
                    "VideoUrlArtifact",
                ],
                type="VideoUrlArtifact",
                tooltip="The media asset to upload (image, audio, or video).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="bucket",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Spaces bucket name.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="key_prefix",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Folder-style prefix for the object key, e.g. gaudi/welcome/. "
                "A trailing slash is added if missing.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="filename",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional filename override. Left empty, the name comes from the artifact "
                "(or a generated name with a content-sniffed extension).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="public",
                type="bool",
                default_value=True,
                tooltip="Make the object publicly readable (required for ViewComfy and browser access).",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="url",
                output_type="str",
                tooltip="Public URL of the uploaded object.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="key",
                output_type="str",
                tooltip="The object key, for downstream deletes or replacements.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the Spaces upload result",
            result_details_placeholder="Upload details will appear here.",
        )

    def validate_before_node_run(self) -> list[Exception] | None:
        try:
            missing = self._missing_secrets()
        except Exception as e:
            return [e]
        if missing:
            return [
                ValueError(
                    f"{', '.join(missing)} not set. Add them under Settings > API Keys & Secrets, "
                    "then restart the engine (newly registered secrets only load at startup)."
                )
            ]
        return None

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        try:
            missing = self._missing_secrets()
            if missing:
                raise ValueError(f"{', '.join(missing)} not set.")
            access_key, secret_key, region, endpoint = self._read_config()

            bucket = (self.parameter_values.get("bucket") or "").strip()
            if not bucket:
                raise ValueError("No bucket name set.")

            data = self._artifact_to_bytes(self.parameter_values.get("artifact"), "artifact")
            mime = _sniff_mime(data, "application/octet-stream")
            filename = self._derive_filename(mime)

            prefix = (self.parameter_values.get("key_prefix") or "").strip().lstrip("/")
            if prefix and not prefix.endswith("/"):
                prefix += "/"
            key = prefix + filename

            public = bool(self.parameter_values.get("public", True))
            self._upload(access_key, secret_key, region, endpoint, bucket, key, data, mime, public)

            host = urlparse(endpoint).netloc
            url = f"https://{bucket}.{host}/{quote(key)}"
            self.parameter_output_values["url"] = url
            self.parameter_output_values["key"] = key

            acl_note = "public-read" if public else "private"
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Uploaded {len(data) / (1024 * 1024):.2f} MB ({mime}, {acl_note}) "
                    f"to {bucket}/{key}\n{url}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    # -- Spaces helpers -----------------------------------------------------

    def _missing_secrets(self) -> list[str]:
        secrets = GriptapeNodes.SecretsManager()

        def read(name: str) -> str:
            try:
                return (secrets.get_secret(name) or "").strip()
            except Exception:
                return ""

        missing = [name for name in (KEY_SECRET_NAME, SECRET_SECRET_NAME) if not read(name)]
        # Region and endpoint are derivable from each other, so only both-absent is fatal.
        if not read(REGION_SECRET_NAME) and not read(ENDPOINT_SECRET_NAME):
            missing.append(f"{REGION_SECRET_NAME} (or {ENDPOINT_SECRET_NAME})")
        return missing

    def _read_config(self) -> tuple[str, str, str, str]:
        secrets = GriptapeNodes.SecretsManager()

        def read(name: str) -> str:
            try:
                return (secrets.get_secret(name) or "").strip()
            except Exception:
                return ""

        access_key = read(KEY_SECRET_NAME)
        secret_key = read(SECRET_SECRET_NAME)
        region = read(REGION_SECRET_NAME)
        endpoint = read(ENDPOINT_SECRET_NAME)
        if endpoint and not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"
        if not endpoint:
            endpoint = f"https://{region}.digitaloceanspaces.com"
        if not region:
            # First host label of the origin endpoint, e.g. nyc3.digitaloceanspaces.com -> nyc3.
            region = urlparse(endpoint).netloc.split(".")[0]
        return access_key, secret_key, region, endpoint

    def _upload(
        self,
        access_key: str,
        secret_key: str,
        region: str,
        endpoint: str,
        bucket: str,
        key: str,
        data: bytes,
        mime: str,
        public: bool,
    ) -> None:
        client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=mime,
                ACL="public-read" if public else "private",
            )
        except EndpointConnectionError as e:
            raise RuntimeError(
                f"Could not reach the Spaces endpoint {endpoint}. "
                f"Check {ENDPOINT_SECRET_NAME}/{REGION_SECRET_NAME} and your network connection."
            ) from e
        except ClientError as e:
            error = e.response.get("Error") or {}
            code = error.get("Code") or "unknown"
            message = error.get("Message") or ""
            if code == "InvalidAccessKeyId":
                raise RuntimeError(
                    f"Spaces rejected the access key (InvalidAccessKeyId). Check {KEY_SECRET_NAME}."
                ) from e
            if code == "SignatureDoesNotMatch":
                raise RuntimeError(
                    f"Spaces rejected the request signature (SignatureDoesNotMatch). Check {SECRET_SECRET_NAME}."
                ) from e
            if code == "NoSuchBucket":
                raise RuntimeError(
                    f"Bucket {bucket!r} does not exist at {endpoint}. "
                    f"Check the bucket name and {REGION_SECRET_NAME}/{ENDPOINT_SECRET_NAME}."
                ) from e
            if code == "AccessDenied":
                raise RuntimeError(
                    f"Access denied uploading to {bucket}/{key}. "
                    "The key pair may lack write permission for this bucket."
                ) from e
            raise RuntimeError(f"Spaces API error during upload [{code}]: {message}") from e

    def _derive_filename(self, mime: str) -> str:
        ext = _EXTENSION_BY_MIME.get(mime)
        explicit = (self.parameter_values.get("filename") or "").strip()
        if explicit:
            name = _sanitize_filename(explicit)
            if "." not in name and ext:
                name = f"{name}.{ext}"
            return name
        artifact = self.parameter_values.get("artifact")
        candidates = [str(getattr(artifact, "name", "") or "")]
        value = getattr(artifact, "value", "")
        if isinstance(value, str):
            candidates.append(value)
        for candidate in candidates:
            if not candidate:
                continue
            if candidate.startswith(("http://", "https://")):
                candidate = urlparse(candidate).path
            base = _sanitize_filename(Path(candidate).name)
            # Only trust names that carry a real extension; artifact ids and bare
            # titles fall through to the generated name below.
            if base and "." in base.strip("."):
                return base
        return f"upload_{uuid.uuid4().hex[:8]}.{ext or 'bin'}"

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
