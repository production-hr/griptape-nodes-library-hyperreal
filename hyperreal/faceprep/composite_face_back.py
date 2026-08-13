from __future__ import annotations

import base64
import json
import logging
import math
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
from PIL import Image, ImageDraw, ImageFilter, ImageStat

logger = logging.getLogger("griptape_nodes")

DOWNLOAD_TIMEOUT_SECONDS = 300
EDGE_SHAPES = ["rounded_rect", "ellipse", "rectangle"]
RESAMPLE_LANCZOS = getattr(Image, "LANCZOS", None) or Image.Resampling.LANCZOS
ASPECT_MISMATCH_NOTE_THRESHOLD = 0.02
DRIFT_NOTE_THRESHOLD = 0.03


class CompositeFaceBack(SuccessFailureNode):
    """Paste an enhanced face crop back onto the full-body source image.

    The still-image twin of Composite Region Back: consumes the crop_region JSON
    emitted by Zoom To Head, resizes the enhanced crop to the original box, and
    blends it in with an eroded, feathered matte and optional color match.

    Generative editors rarely preserve composition exactly — the re-rendered head
    usually occupies a different fraction of the canvas than the original did. Wire
    a second Zoom To Head (run on the enhanced image) into enhanced_face_region and
    the paste is auto-corrected: scale from the face-box diagonal ratio, position
    from the face centers — the same anchors Overlay Zoomed Video measured best.
    scale_adjust / offset_x / offset_y are manual nudges applied on top.

    Pure PIL by design — resize, matte, and color transfer need no OpenCV or
    numpy here. Battle-tested via the sandbox on the Stan Lee and Whitney
    face-enhance workflows before graduating into the library.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "image/faceprep",
            "description": "Paste an enhanced face crop back onto the full-body source image, "
            "feathered, color-matched, and auto-aligned by face when a detection of the "
            "enhanced image is wired in.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="base_image",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageArtifact",
                tooltip="The original full-body image the crop was taken from.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="face_image",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageArtifact",
                tooltip="The enhanced face crop to paste back (any resolution).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="crop_region",
                input_types=["json"],
                type="json",
                tooltip="The crop_region from the Zoom To Head that made the original crop.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="enhanced_face_region",
                input_types=["json"],
                type="json",
                tooltip="Optional but strongly recommended: crop_region from a second Zoom To Head run "
                "on the ENHANCED image. Enables auto scale/position correction for generator "
                "composition drift. Without it, the canvas is pasted box-to-box as-is.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="edge_shape",
                type="str",
                default_value="rounded_rect",
                tooltip="Matte shape. rounded_rect keeps most of the enhanced crop; ellipse hides seams best; "
                "rectangle keeps everything up to the feather.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=EDGE_SHAPES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="feather_px",
                input_types=["int"],
                type="int",
                default_value=24,
                tooltip="How softly the paste edge lands, in pixels of gaussian feather.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_inset_px",
                input_types=["int"],
                type="int",
                default_value=8,
                tooltip="Erode: pull the matte edge inward this many pixels before feathering, so the blend "
                "never samples the very edge of the enhanced crop.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="extend_top_coverage",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip="When the aligned insert lands lower than the original crop box (generator zoomed in), "
                "extend it upward with backdrop replicated from its own top edge so the original hair "
                "crown is fully replaced. Assumes a clean studio backdrop above the head — disable if "
                "the top of the enhanced image is not background.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="scale_adjust",
                input_types=["float"],
                type="float",
                default_value=1.0,
                tooltip="Manual scale nudge on top of the automatic result (1.0 = no change). Scales about "
                "the face anchor when auto-align is active, about the box center otherwise.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="offset_x",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Manual horizontal nudge in base-image pixels (positive = right).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="offset_y",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Manual vertical nudge in base-image pixels (positive = down).",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="color_match",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip="Match the enhanced crop's per-channel mean and contrast to the original region before "
                "pasting. Two generations rarely agree exactly on exposure.",
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
                tooltip="The full-body image with the enhanced face blended in.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_preview",
                output_type="ImageUrlArtifact",
                tooltip="Full-frame view of where the blend lands (white = enhanced content). Check this "
                "when tuning scale, feather, and inset.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the face paste-back",
            result_details_placeholder="Composite details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        self._saved_files: list[str] = []
        try:
            base = self._open_rgb(self.parameter_values.get("base_image"), "base_image")
            face = self._open_rgb(self.parameter_values.get("face_image"), "face_image")
            box, source, orig_face = self._parse_region(self.parameter_values.get("crop_region"))

            notes: list[str] = []
            if source is not None and (base.width, base.height) != (source["width"], source["height"]):
                raise ValueError(
                    f"crop_region says the source image was {source['width']}x{source['height']}, but "
                    f"base_image is {base.width}x{base.height}. This region belongs to a different image."
                )

            x, y, w, h = box["x"], box["y"], box["width"], box["height"]
            if w <= 0 or h <= 0:
                raise ValueError(f"crop_region box has a non-positive size ({w}x{h}).")

            paste = self._compute_placement(face, x, y, w, h, orig_face, notes)
            insert_w, insert_h, paste_x, paste_y = paste

            box_aspect, face_aspect = w / h, face.width / face.height
            if abs(face_aspect - box_aspect) / box_aspect > ASPECT_MISMATCH_NOTE_THRESHOLD:
                notes.append(
                    f"face_image aspect ({face_aspect:.3f}) differs from the crop box ({box_aspect:.3f}) — "
                    "check the generator's aspect_ratio setting"
                )

            insert = face.resize((insert_w, insert_h), RESAMPLE_LANCZOS)
            if bool(self.parameter_values.get("color_match", True)):
                bx0, by0 = max(0, paste_x), max(0, paste_y)
                bx1 = min(base.width, paste_x + insert_w)
                by1 = min(base.height, paste_y + insert_h)
                if bx1 > bx0 and by1 > by0:
                    insert, match_note = self._match_color(insert, base.crop((bx0, by0, bx1, by1)))
                    notes.append(match_note)

            if bool(self.parameter_values.get("extend_top_coverage", True)):
                insert, paste_y, insert_h, ext_note = self._extend_top(insert, paste_y, y)
                if ext_note:
                    notes.append(ext_note)

            matte = self._build_matte(insert_w, insert_h)
            result = base.copy()
            result.paste(insert, (paste_x, paste_y), matte)

            preview = Image.new("L", (base.width, base.height), 0)
            preview.paste(matte, (paste_x, paste_y))

            self.parameter_output_values["composited_image"] = self._publish(result, "face_composite", copy_out=True)
            self.parameter_output_values["matte_preview"] = self._publish(preview.convert("RGB"), "face_matte")

            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            note_text = ("\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Pasted {face.width}x{face.height} enhanced crop as {insert_w}x{insert_h} at "
                    f"({paste_x}, {paste_y}) on a {base.width}x{base.height} base "
                    f"({self.parameter_values.get('edge_shape')}, "
                    f"feather {self.parameter_values.get('feather_px')}px, "
                    f"inset {self.parameter_values.get('matte_inset_px')}px).{note_text}{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    # -- Placement -----------------------------------------------------------

    def _compute_placement(
        self,
        face: Image.Image,
        x: int,
        y: int,
        w: int,
        h: int,
        orig_face: dict[str, float] | None,
        notes: list[str],
    ) -> tuple[int, int, int, int]:
        """Return (insert_w, insert_h, paste_x, paste_y) in base-image pixels.

        Default is the naive box mapping. With an enhanced-face detection wired in,
        scale comes from the face-box diagonal ratio and position from the face
        centers, correcting generator composition drift.
        """
        scale_adjust = float(self.parameter_values.get("scale_adjust") or 1.0)
        offset_x = int(self.parameter_values.get("offset_x") or 0)
        offset_y = int(self.parameter_values.get("offset_y") or 0)

        enh_face = self._parse_enhanced_face(face, notes)
        if enh_face is not None and orig_face is not None:
            d_o = math.hypot(orig_face["width"], orig_face["height"])
            d_e = math.hypot(enh_face["width"], enh_face["height"])
            if d_e > 1e-6:
                k = (d_o / d_e) * scale_adjust
                naive_drift = (d_e * (w / face.width)) / d_o
                if abs(naive_drift - 1.0) > DRIFT_NOTE_THRESHOLD:
                    notes.append(
                        f"auto-align: generator drift measured — naive paste would render the face "
                        f"{naive_drift:.2f}x its original size; corrected by {1 / naive_drift:.2f}x"
                    )
                ocx = orig_face["x"] + orig_face["width"] / 2
                ocy = orig_face["y"] + orig_face["height"] / 2
                ecx = (enh_face["x"] + enh_face["width"] / 2) * k
                ecy = (enh_face["y"] + enh_face["height"] / 2) * k
                insert_w = max(1, round(face.width * k))
                insert_h = max(1, round(face.height * k))
                paste_x = round(ocx - ecx) + offset_x
                paste_y = round(ocy - ecy) + offset_y
                return insert_w, insert_h, paste_x, paste_y
            notes.append("auto-align skipped: enhanced face detection had zero size")
        elif enh_face is not None and orig_face is None:
            notes.append(
                "auto-align skipped: crop_region has no 'face' entry — re-run the original Zoom To Head "
                "(older regions lack it)"
            )

        # Naive box mapping, with manual nudges applied about the box center.
        insert_w = max(1, round(w * scale_adjust))
        insert_h = max(1, round(h * scale_adjust))
        paste_x = x + (w - insert_w) // 2 + offset_x
        paste_y = y + (h - insert_h) // 2 + offset_y
        return insert_w, insert_h, paste_x, paste_y

    def _extend_top(
        self, insert: Image.Image, paste_y: int, box_top: int
    ) -> tuple[Image.Image, int, int, str | None]:
        """Stretch the insert's own top-edge backdrop upward to the original crop-box top.

        Covers the case where the generator zoomed in: the face-aligned insert starts
        below the original hair crown, which would otherwise survive as a ghost fringe.
        """
        ext = paste_y - box_top
        if ext <= 0:
            return insert, paste_y, insert.height, None
        strip = insert.crop((0, 0, insert.width, max(1, insert.height // 50)))
        backdrop = strip.resize((insert.width, ext), RESAMPLE_LANCZOS)
        extended = Image.new("RGB", (insert.width, insert.height + ext))
        extended.paste(backdrop, (0, 0))
        extended.paste(insert, (0, ext))
        note = (
            f"extended coverage {ext}px above the insert with backdrop replicated from its top edge, "
            "burying the original hair crown (disable via extend_top_coverage)"
        )
        return extended, box_top, extended.height, note

    def _parse_enhanced_face(self, face_image: Image.Image, notes: list[str]) -> dict[str, float] | None:
        value = self.parameter_values.get("enhanced_face_region")
        if value is None:
            return None
        region = getattr(value, "value", value)
        if isinstance(region, str):
            region = json.loads(region)
        if not isinstance(region, dict):
            notes.append("auto-align skipped: enhanced_face_region is not a JSON object")
            return None
        enh_face = region.get("face")
        if not isinstance(enh_face, dict):
            notes.append("auto-align skipped: enhanced_face_region has no 'face' entry")
            return None
        parsed = {k: float(enh_face[k]) for k in ("x", "y", "width", "height")}
        # The detector may have seen the image at a different size than face_image; rescale.
        source = region.get("source")
        if isinstance(source, dict) and source.get("width"):
            ratio = face_image.width / float(source["width"])
            if abs(ratio - 1.0) > 1e-6:
                parsed = {k: v * ratio for k, v in parsed.items()}
        return parsed

    # -- Compositing ---------------------------------------------------------

    def _build_matte(self, w: int, h: int) -> Image.Image:
        feather = max(0, int(self.parameter_values.get("feather_px") or 0))
        inset = max(0, int(self.parameter_values.get("matte_inset_px") or 0))
        # The blur eats inward roughly half its radius; keep the drawn shape clear of the edge.
        margin = inset + (feather // 2)
        margin = min(margin, (min(w, h) - 2) // 2)  # never invert the box on tiny crops
        shape_box = (margin, margin, w - 1 - margin, h - 1 - margin)

        matte = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(matte)
        shape = self.parameter_values.get("edge_shape") or "rounded_rect"
        if shape == "ellipse":
            draw.ellipse(shape_box, fill=255)
        elif shape == "rectangle":
            draw.rectangle(shape_box, fill=255)
        else:
            draw.rounded_rectangle(shape_box, radius=max(1, min(w, h) // 8), fill=255)
        if feather > 0:
            matte = matte.filter(ImageFilter.GaussianBlur(feather))
        return matte

    def _match_color(self, insert: Image.Image, base_region: Image.Image) -> tuple[Image.Image, str]:
        """Per-channel linear transfer: move the insert's mean/contrast onto the base region's."""
        insert_stat = ImageStat.Stat(insert)
        base_stat = ImageStat.Stat(base_region)
        lut: list[int] = []
        shifts: list[float] = []
        for channel in range(3):
            mean_i, mean_b = insert_stat.mean[channel], base_stat.mean[channel]
            std_i, std_b = insert_stat.stddev[channel], base_stat.stddev[channel]
            gain = (std_b / std_i) if std_i > 1e-6 else 1.0
            offset = mean_b - gain * mean_i
            shifts.append(mean_b - mean_i)
            lut.extend(min(255, max(0, round(gain * v + offset))) for v in range(256))
        matched = insert.point(lut)
        note = "color match applied (mean shift R{:+.1f} G{:+.1f} B{:+.1f})".format(*shifts)
        return matched, note

    def _parse_region(
        self, value: Any
    ) -> tuple[dict[str, int], dict[str, int] | None, dict[str, float] | None]:
        if value is None:
            raise ValueError("No crop_region input connected — wire it from Zoom To Head.")
        region = getattr(value, "value", value)
        if isinstance(region, str):
            region = json.loads(region)
        if not isinstance(region, dict):
            raise ValueError(f"crop_region must be a JSON object, got {type(region).__name__}.")
        box = region.get("box", region)
        try:
            parsed_box = {k: int(box[k]) for k in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(
                "crop_region needs x, y, width, height (directly or under 'box', as Zoom To Head emits)."
            ) from e
        source = region.get("source")
        parsed_source = None
        if isinstance(source, dict) and "width" in source and "height" in source:
            parsed_source = {"width": int(source["width"]), "height": int(source["height"])}
        face = region.get("face")
        parsed_face = None
        if isinstance(face, dict) and all(k in face for k in ("x", "y", "width", "height")):
            parsed_face = {k: float(face[k]) for k in ("x", "y", "width", "height")}
        return parsed_box, parsed_source, parsed_face

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
        """Optionally write the composite into the user-chosen folder; failures are reported, not fatal."""
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
