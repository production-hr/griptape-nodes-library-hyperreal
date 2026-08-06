from __future__ import annotations

import base64
import logging
import math
import tempfile
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

DOWNLOAD_TIMEOUT_SECONDS = 300
MODEL_PATH = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
ASPECT_RATIOS = ["source", "square", "9:16", "16:9", "4:5", "3:4"]
ASPECT_VALUES = {"square": 1.0, "9:16": 9 / 16, "16:9": 16 / 9, "4:5": 4 / 5, "3:4": 3 / 4}


class ZoomToHead(SuccessFailureNode):
    """Crop a head-and-shoulders framing out of a full-body image.

    The zoomed source for a second lipsync pass: lipsyncing this crop gives a
    face with many more pixels than lipsyncing the full-body frame, and the
    result can be blended back over the full-body render.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "image/faceprep",
            "description": "Crop a head-and-shoulders zoom from a full-body image, for a higher-quality "
            "lipsync pass.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="image",
                input_types=["ImageArtifact", "ImageUrlArtifact"],
                type="ImageArtifact",
                tooltip="The full-body source image.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="pad_top",
                input_types=["float"],
                type="float",
                default_value=1.2,
                tooltip="Headroom above the face, as a fraction of face height. Raise for big hair or a hat.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="pad_bottom",
                input_types=["float"],
                type="float",
                default_value=2.6,
                tooltip="How far below the chin to include, as a fraction of face height. The default reaches "
                "roughly mid-chest for a head-and-shoulders framing.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="pad_sides",
                input_types=["float"],
                type="float",
                default_value=1.4,
                tooltip="Width either side of the face, as a fraction of face width.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="aspect_ratio",
                type="str",
                default_value="source",
                tooltip="Shape of the crop. 'source' keeps the input image's aspect ratio, which is usually "
                "what you want so the lipsync render matches the full-body one.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=ASPECT_RATIOS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_long_edge",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Resize the crop so its long edge is this many pixels. 0 keeps the crop's native "
                "pixels (no resampling) — best quality, and normally the right choice.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="subject_index",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="0 = highest-confidence face; use 1, 2... to pick another face in a group shot.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="confidence_threshold",
                input_types=["float"],
                type="float",
                default_value=0.6,
                tooltip="Minimum detector confidence for a face to count.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save the zoomed image into, e.g. {project_dir}/outputs.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="zoomed_image",
                output_type="ImageUrlArtifact",
                tooltip="The head-and-shoulders crop — feed this to the lipsync node.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="preview_image",
                output_type="ImageUrlArtifact",
                tooltip="The source image with the crop box drawn on it. Check this while dialling in the pads.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="crop_region",
                output_type="json",
                tooltip="The crop box in source-image coordinates, plus the measured zoom factor.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="zoom_factor",
                output_type="float",
                tooltip="How much larger the face is in the crop than in the source framing. Aim for 2x or more.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the zoom crop",
            result_details_placeholder="Crop details will appear here.",
        )

    def validate_before_node_run(self) -> list[Exception] | None:
        if not MODEL_PATH.is_file():
            return [FileNotFoundError(f"Vendored detector weights missing: {MODEL_PATH}")]
        return None

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        self._saved_files: list[str] = []
        temp_files: list[Path] = []
        try:
            data = self._artifact_to_bytes(self.parameter_values.get("image"), "image")
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("Could not decode the input image.")
            height, width = image.shape[:2]

            face = self._detect_face(image)
            box = self._crop_box(face, width, height)
            crop = image[box["y"] : box["y"] + box["height"], box["x"] : box["x"] + box["width"]]

            long_edge = int(self.parameter_values.get("output_long_edge") or 0)
            resized_note = ""
            if long_edge > 0:
                scale = long_edge / max(crop.shape[0], crop.shape[1])
                target = (max(1, int(round(crop.shape[1] * scale))), max(1, int(round(crop.shape[0] * scale))))
                interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LANCZOS4
                crop = cv2.resize(crop, target, interpolation=interpolation)
                resized_note = f", resized to {target[0]}x{target[1]}"

            zoom_factor = width / box["width"]
            region = {
                "schema": "hyperreal.zoom_crop/1",
                "source": {"width": width, "height": height},
                "box": {k: box[k] for k in ("x", "y", "width", "height")},
                "face": {"x": face[0], "y": face[1], "width": face[2], "height": face[3]},
                "zoom_factor": round(zoom_factor, 4),
                "detector": "yunet-2023mar",
            }

            crop_path = Path(tempfile.gettempdir()) / f"zoom_{uuid.uuid4().hex}.png"
            preview_path = Path(tempfile.gettempdir()) / f"zoom_preview_{uuid.uuid4().hex}.png"
            temp_files += [crop_path, preview_path]
            cv2.imwrite(str(crop_path), crop)

            preview = image.copy()
            thickness = max(2, width // 400)
            cv2.rectangle(
                preview, (box["x"], box["y"]),
                (box["x"] + box["width"], box["y"] + box["height"]), (0, 200, 255), thickness,
            )
            cv2.rectangle(
                preview, (int(face[0]), int(face[1])),
                (int(face[0] + face[2]), int(face[1] + face[3])), (0, 255, 0), thickness,
            )
            cv2.imwrite(str(preview_path), preview)

            self.parameter_output_values["zoomed_image"] = self._publish(crop_path, "zoom", copy_out=True)
            self.parameter_output_values["preview_image"] = self._publish(preview_path, "zoom_preview")
            self.parameter_output_values["crop_region"] = region
            self.parameter_output_values["zoom_factor"] = round(zoom_factor, 4)

            warning = ""
            if zoom_factor < 2.0:
                warning = (
                    f"\nNOTE: the face is only {zoom_factor:.2f}x larger than in the source framing. "
                    "Tighten the pads for a bigger quality gain — 2x or more is the point of this pass."
                )
            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Cropped {box['width']}x{box['height']} from {width}x{height} at "
                    f"({box['x']}, {box['y']}){resized_note}; face is {zoom_factor:.2f}x larger than in the "
                    f"source framing.{warning}{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            for path in temp_files:
                path.unlink(missing_ok=True)

    # -- Detection and framing ----------------------------------------------

    def _detect_face(self, image: np.ndarray) -> tuple[float, float, float, float]:
        height, width = image.shape[:2]
        threshold = float(self.parameter_values.get("confidence_threshold") or 0.6)
        detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (width, height), threshold)
        detector.setInputSize((width, height))
        faces = detector.detect(image)[1]
        if faces is None or len(faces) == 0:
            raise RuntimeError(
                f"No face found in the image at confidence {threshold}. Lower confidence_threshold, or check "
                "that the image actually shows a face."
            )
        ranked = sorted(faces, key=lambda f: float(f[14]), reverse=True)
        index = max(0, int(self.parameter_values.get("subject_index") or 0))
        if index >= len(ranked):
            raise RuntimeError(
                f"subject_index {index} was requested but only {len(ranked)} face(s) were found."
            )
        chosen = ranked[index]
        return (float(chosen[0]), float(chosen[1]), float(chosen[2]), float(chosen[3]))

    def _crop_box(self, face: tuple[float, float, float, float], width: int, height: int) -> dict[str, int]:
        fx, fy, fw, fh = face
        pad_top = float(self.parameter_values.get("pad_top") or 0.0)
        pad_bottom = float(self.parameter_values.get("pad_bottom") or 0.0)
        pad_sides = float(self.parameter_values.get("pad_sides") or 0.0)

        x0 = fx - pad_sides * fw
        x1 = fx + fw + pad_sides * fw
        y0 = fy - pad_top * fh
        y1 = fy + fh + pad_bottom * fh

        aspect = self.parameter_values.get("aspect_ratio") or "source"
        target = (width / height) if aspect == "source" else ASPECT_VALUES.get(aspect, width / height)

        box_w, box_h = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # Grow (never shrink) to the target aspect so nothing the pads asked for is lost.
        if box_w / box_h < target:
            box_w = box_h * target
        else:
            box_h = box_w / target

        # Clamp into frame, shrinking on the target aspect if the box is larger than the image.
        if box_w > width:
            box_w, box_h = float(width), width / target
        if box_h > height:
            box_h, box_w = float(height), height * target
        x0 = min(max(cx - box_w / 2, 0), width - box_w)
        y0 = min(max(cy - box_h / 2, 0), height - box_h)
        return {
            "x": int(round(x0)),
            "y": int(round(y0)),
            "width": int(math.floor(box_w)),
            "height": int(math.floor(box_h)),
        }

    # -- Output --------------------------------------------------------------

    def _publish(self, path: Path, label: str, *, copy_out: bool = False) -> ImageUrlArtifact:
        data = path.read_bytes()
        filename = f"{label}_{uuid.uuid4().hex[:8]}.png"
        saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
        if copy_out:
            self._save_copy_to_output_directory(data, filename)
        return ImageUrlArtifact(value=saved_url, name=filename)

    def _save_copy_to_output_directory(self, data: bytes, filename: str) -> None:
        """Optionally write the crop into the user-chosen folder; failures are reported, not fatal."""
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
            logger.warning("Could not save zoom crop to %r", directory, exc_info=True)
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
