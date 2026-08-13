from __future__ import annotations

import base64
import logging
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from griptape.artifacts.image_url_artifact import ImageUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options
from PIL import Image, ImageStat

logger = logging.getLogger("griptape_nodes")

DOWNLOAD_TIMEOUT_SECONDS = 300

# Model guidance: birefnet-portrait is the right default for our people-on-backdrop
# masters (best soft hair edges); birefnet-general for non-person subjects;
# isnet-general-use and u2net are lighter/faster fallbacks with coarser edges.
MATTING_MODELS = ["birefnet-portrait", "birefnet-general", "isnet-general-use", "u2net"]

# Model sessions are expensive to build (weights load); cache per model name for
# the life of the engine process.
_SESSIONS: dict[str, Any] = {}

COVERAGE_WARN_LOW = 0.02
COVERAGE_WARN_HIGH = 0.98


class ExtractImageMatte(SuccessFailureNode):
    """Extract a soft-alpha matte from a still image with rembg (BiRefNet et al).

    Local inference, no API, no secrets. Built for pulling mattes off the
    gray-backdrop AI masters: outputs the RGBA cutout and the matte itself
    (white = subject), ready for Resolve/Nuke or the Composite nodes.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "image/matte",
            "description": "Extract a soft-alpha matte and RGBA cutout from a still image "
            "using rembg/BiRefNet, locally.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="image",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageArtifact",
                tooltip="The image to matte. A subject on an even studio backdrop mattes best.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="model",
                type="str",
                default_value="birefnet-portrait",
                tooltip="birefnet-portrait: best for people, softest hair edges (default). "
                "birefnet-general: non-person subjects. isnet-general-use / u2net: lighter "
                "and faster, coarser edges. First use of a model downloads its weights once.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=MATTING_MODELS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="alpha_matting",
                input_types=["bool"],
                type="bool",
                default_value=False,
                tooltip="Extra alpha-matting refinement pass on the mask edge. The BiRefNet "
                "models rarely need it; try it if fine hair edges come back hard.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the cutout and matte into, e.g. {project_dir}/outputs.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="cutout_image",
                output_type="ImageUrlArtifact",
                tooltip="The subject cut out on transparency (RGBA PNG).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_image",
                output_type="ImageUrlArtifact",
                tooltip="The matte alone: white = subject, black = background, gray = soft edge. "
                "Same polarity as the Composite nodes and Resolve/Nuke defaults.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the matte extraction",
            result_details_placeholder="Matte details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        self._saved_files: list[str] = []
        try:
            try:
                from rembg import new_session, remove
            except ImportError as e:
                raise RuntimeError(
                    "The rembg package is not installed in the library environment. "
                    "Refresh Libraries (or restart the engine) so the manifest's new "
                    "dependencies are installed, then run again."
                ) from e

            data = self._artifact_to_bytes(self.parameter_values.get("image"), "image")
            try:
                source = Image.open(BytesIO(data)).convert("RGB")
            except Exception as e:
                raise ValueError(f"Could not decode the input image: {e}") from e

            model = self.parameter_values.get("model") or "birefnet-portrait"
            if model not in _SESSIONS:
                logger.info("Loading matting model %r (first use downloads its weights once)", model)
                _SESSIONS[model] = new_session(model)

            kwargs: dict[str, Any] = {"session": _SESSIONS[model]}
            if bool(self.parameter_values.get("alpha_matting", False)):
                kwargs.update(
                    alpha_matting=True,
                    alpha_matting_foreground_threshold=240,
                    alpha_matting_background_threshold=10,
                    alpha_matting_erode_size=10,
                )

            cutout = remove(source, **kwargs)
            if cutout.mode != "RGBA":
                cutout = cutout.convert("RGBA")
            matte = cutout.split()[3]

            coverage = ImageStat.Stat(matte).mean[0] / 255.0
            notes: list[str] = []
            if coverage < COVERAGE_WARN_LOW:
                notes.append(
                    f"matte is nearly empty ({coverage:.1%} coverage) — the model may not have found "
                    "a subject; try a different model"
                )
            elif coverage > COVERAGE_WARN_HIGH:
                notes.append(
                    f"matte is nearly full-frame ({coverage:.1%} coverage) — the model may have kept "
                    "the background; try a different model"
                )

            self.parameter_output_values["cutout_image"] = self._publish(cutout, "matte_cutout", copy_out=True)
            self.parameter_output_values["matte_image"] = self._publish(
                matte.convert("RGB"), "matte", copy_out=True
            )

            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            note_text = ("\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Matted {source.width}x{source.height} with {model}; subject covers "
                    f"{coverage:.1%} of the frame.{note_text}{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    # -- Output --------------------------------------------------------------

    def _publish(self, image: Image.Image, label: str, *, copy_out: bool = False) -> ImageUrlArtifact:
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        filename = f"{label}_{uuid.uuid4().hex[:8]}.png"
        saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
        if copy_out:
            self._save_copy_to_output_directory(data, filename)
        return ImageUrlArtifact(value=saved_url, name=filename)

    def _save_copy_to_output_directory(self, data: bytes, filename: str) -> None:
        """Optionally write outputs into the user-chosen folder; failures are reported, not fatal."""
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
            logger.warning("Could not save matte output to %r", directory, exc_info=True)
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

    # -- Media input handling (copied from hyperreal/faceprep/zoom_to_head.py;
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
