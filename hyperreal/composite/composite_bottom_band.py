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
from PIL import Image

logger = logging.getLogger("griptape_nodes")

DOWNLOAD_TIMEOUT_SECONDS = 300
RESAMPLE_LANCZOS = getattr(Image, "LANCZOS", None) or Image.Resampling.LANCZOS


class CompositeBottomBand(SuccessFailureNode):
    """Composite the bottom band of a full-frame AI edit over the original image.

    Built for floor treatments (reflections, shadows, floor swaps): the generator
    edits the WHOLE image so it can see the subject it is mirroring, then only the
    bottom fraction of its output is blended back — everything above the seam stays
    guaranteed-original. The edited frame is resized to the base's exact dimensions
    first, so the band is pixel-aligned by construction.

    The matte is opaque everywhere except a linear fade along its TOP edge — the
    left, right, and bottom edges coincide with the image borders, where feathering
    would just fade the edit back out.

    Pure PIL by design — the resize, band crop, and gradient matte need no
    OpenCV or numpy. Battle-tested via the sandbox on the Stan Lee floor
    reflection workflow before graduating into the library.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "image/composite",
            "description": "Blend the bottom band of a full-frame AI edit (e.g. an added floor "
            "reflection) over the original image, feathered only along the top seam.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="base_image",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageArtifact",
                tooltip="The original, untouched image.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="edited_image",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageArtifact",
                tooltip="The full-frame AI edit of the same image (any resolution; it is resized to match).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="band_fraction",
                input_types=["float"],
                type="float",
                default_value=0.25,
                tooltip="How much of the frame height to take from the edit, measured up from the bottom "
                "(0.25 = bottom quarter). Set it so the seam lands just above the shoes.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="feather_px",
                input_types=["int"],
                type="int",
                default_value=48,
                tooltip="Height of the linear fade along the band's top seam, in pixels of the base image.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="color_match",
                input_types=["bool"],
                type="bool",
                default_value=False,
                tooltip="Match the band's tone to the original band before pasting. OFF by default: the "
                "band intentionally differs from the original (it contains the new reflection), and "
                "matching would dim exactly the thing you added. Enable only if the edit shifted the "
                "overall floor tone.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the composited image into, e.g. {project_dir}/outputs.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="composited_image",
                output_type="ImageUrlArtifact",
                tooltip="The original image with the edited bottom band blended in.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_preview",
                output_type="ImageUrlArtifact",
                tooltip="Full-frame view of the blend (white = edited content). Check the seam height here.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="band_region",
                output_type="json",
                tooltip="The band in base-image coordinates, for reference or downstream nodes.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the band composite",
            result_details_placeholder="Composite details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        self._saved_files: list[str] = []
        try:
            base = self._open_rgb(self.parameter_values.get("base_image"), "base_image")
            edited = self._open_rgb(self.parameter_values.get("edited_image"), "edited_image")

            fraction = float(self.parameter_values.get("band_fraction") or 0.25)
            if not 0.02 <= fraction <= 0.9:
                raise ValueError(f"band_fraction {fraction} is outside the sensible range 0.02-0.9.")

            notes: list[str] = []
            base_aspect = base.width / base.height
            edited_aspect = edited.width / edited.height
            if abs(edited_aspect - base_aspect) / base_aspect > 0.02:
                notes.append(
                    f"edited_image aspect ({edited_aspect:.3f}) differs from base ({base_aspect:.3f}); "
                    "the resize stretches it slightly — check the generator's aspect_ratio setting"
                )
            if (edited.width, edited.height) != (base.width, base.height):
                edited = edited.resize((base.width, base.height), RESAMPLE_LANCZOS)

            band_h = max(1, round(base.height * fraction))
            band_top = base.height - band_h
            band = edited.crop((0, band_top, base.width, base.height))

            if bool(self.parameter_values.get("color_match", False)):
                band, match_note = self._match_color(band, base.crop((0, band_top, base.width, base.height)))
                notes.append(match_note)

            matte = self._build_matte(base.width, band_h)
            result = base.copy()
            result.paste(band, (0, band_top), matte)

            preview = Image.new("L", (base.width, base.height), 0)
            preview.paste(matte, (0, band_top))

            region = {
                "schema": "hyperreal.bottom_band/1",
                "source": {"width": base.width, "height": base.height},
                "box": {"x": 0, "y": band_top, "width": base.width, "height": band_h},
                "band_fraction": fraction,
            }

            self.parameter_output_values["composited_image"] = self._publish(result, "band_composite", copy_out=True)
            self.parameter_output_values["matte_preview"] = self._publish(preview.convert("RGB"), "band_matte")
            self.parameter_output_values["band_region"] = region

            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            note_text = ("\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Blended the bottom {band_h}px ({fraction:.0%}) of the edit over the "
                    f"{base.width}x{base.height} base, seam feather "
                    f"{self.parameter_values.get('feather_px')}px.{note_text}{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    # -- Compositing ---------------------------------------------------------

    def _build_matte(self, w: int, h: int) -> Image.Image:
        """Opaque band with a linear fade along the top edge only."""
        feather = max(0, int(self.parameter_values.get("feather_px") or 0))
        feather = min(feather, h - 1)
        matte = Image.new("L", (w, h), 255)
        if feather > 0:
            gradient = Image.new("L", (1, feather))
            gradient.putdata([round(255 * i / feather) for i in range(feather)])
            matte.paste(gradient.resize((w, feather)), (0, 0))
        return matte

    def _match_color(self, band: Image.Image, base_band: Image.Image) -> tuple[Image.Image, str]:
        from PIL import ImageStat

        band_stat = ImageStat.Stat(band)
        base_stat = ImageStat.Stat(base_band)
        lut: list[int] = []
        shifts: list[float] = []
        for channel in range(3):
            mean_i, mean_b = band_stat.mean[channel], base_stat.mean[channel]
            std_i, std_b = band_stat.stddev[channel], base_stat.stddev[channel]
            gain = (std_b / std_i) if std_i > 1e-6 else 1.0
            offset = mean_b - gain * mean_i
            shifts.append(mean_b - mean_i)
            lut.extend(min(255, max(0, round(gain * v + offset))) for v in range(256))
        matched = band.point(lut)
        note = "color match applied (mean shift R{:+.1f} G{:+.1f} B{:+.1f})".format(*shifts)
        return matched, note

    def _open_rgb(self, artifact: Any, label: str) -> Image.Image:
        data = self._artifact_to_bytes(artifact, label)
        try:
            return Image.open(BytesIO(data)).convert("RGB")
        except Exception as e:
            raise ValueError(f"Could not decode {label} as an image: {e}") from e

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
            logger.warning("Could not save composite to %r", directory, exc_info=True)
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
