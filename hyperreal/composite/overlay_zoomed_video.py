from __future__ import annotations

import base64
import logging
import math
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from griptape.artifacts.image_url_artifact import ImageUrlArtifact
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

DOWNLOAD_TIMEOUT_SECONDS = 600
FFMPEG_TIMEOUT_SECONDS = 3600
PROBE_TIMEOUT_SECONDS = 300
# Shared with the Face Prep nodes: a vendored data asset, not a code import.
MODEL_PATH = Path(__file__).parent.parent / "faceprep" / "models" / "face_detection_yunet_2023mar.onnx"
ALIGN_MODES = ["auto", "manual"]
EDGE_SHAPES = ["ellipse", "rounded_rect", "rectangle"]
AUDIO_SOURCES = ["base", "overlay", "none"]
DETECT_SAMPLE_FRAMES = 9
REFINE_SAMPLE_FRAMES = 3
REFINE_MIN_CONFIDENCE = 0.25
REFINE_MAX_SHIFT_PX = 40.0


def _ffmpeg_paths() -> tuple[str, str]:
    """(ffmpeg, ffprobe) executables; static_ffmpeg downloads them on first use."""
    import static_ffmpeg.run

    return static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()


def _encoder_color_args(path: Path) -> list[str]:
    """Keep the source's colour matrix across a rawvideo round trip.

    Decoding to bgr24 uses the source's tagged matrix, but encoding raw frames
    back defaults to BT.601. On BT.709 source that shifts every pixel, touched or
    not — measured on a saturated plate, green 160 -> 142 and red clipped to 0.
    """
    _, ffprobe = _ffmpeg_paths()
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=color_space,color_primaries,color_transfer",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False,
    )
    tags: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition("=")
        value = value.strip()
        if value and value not in ("unknown", "reserved", "N/A"):
            tags[key.strip()] = value
    if not tags:
        # Untagged source: ffmpeg uses the same default on decode and encode, so the
        # round trip is already symmetric. Forcing a matrix here would introduce the
        # very shift this function exists to prevent.
        return []
    return [
        "-colorspace", tags.get("color_space", "bt709"),
        "-color_primaries", tags.get("color_primaries", "bt709"),
        "-color_trc", tags.get("color_transfer", "bt709"),
    ]


class OverlayZoomedVideo(SuccessFailureNode):
    """Overlay a zoomed-in lipsync video onto the full-body version of the same performance.

    Lipsyncing a full-body frame gives a small, soft face. Lipsyncing a zoomed
    crop gives a good face in the wrong framing. This node scales the zoomed
    render back down and blends it over the full-body one, deriving the scale
    and position by detecting the head in both clips so nothing has to be
    placed by hand.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/composite",
            "description": "Overlay a zoomed-in lipsync video over the full-body version, auto-aligned by face.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="base_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The full-body lipsync video (the one with the small face).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="overlay_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The zoomed-in lipsync video (same audio, same length, better face).",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="align_mode",
                type="str",
                default_value="auto",
                tooltip="auto detects the head in both clips and matches them. manual uses manual_scale / "
                "manual_center_x / manual_center_y instead.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=ALIGN_MODES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="scale_adjust",
                input_types=["float"],
                type="float",
                default_value=1.0,
                tooltip="Multiplier on the auto-computed scale. 1.0 = heads exactly the same size; "
                "raise slightly if the overlay face reads a touch small.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="offset_x",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Nudge the overlay horizontally, in base-video pixels. Positive moves right.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="offset_y",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Nudge the overlay vertically, in base-video pixels. Positive moves down.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="refine_alignment",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip="After the face-detection estimate, register the two images directly to correct the "
                "last pixel or two of offset. Skipped automatically when the two renders differ too much "
                "for the match to be trusted.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="coverage",
                input_types=["float"],
                type="float",
                default_value=1.0,
                tooltip="How much of the zoomed framing to use, as a fraction of its full frame. "
                "1.0 uses all of it; lower tightens the blend around the face.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="edge_shape",
                type="str",
                default_value="ellipse",
                tooltip="Blend edge. ellipse hides a seam best; rounded_rect and rectangle keep more of the "
                "zoomed framing.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=EDGE_SHAPES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="feather_px",
                input_types=["int"],
                type="int",
                default_value=64,
                tooltip="Feather width in base-video pixels. Generous is good here — the two renders are "
                "separate generations and never match perfectly at the seam.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="color_match",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip="Match the overlay's colour to the base underneath it, per frame, in LAB. On by "
                "default because two separate generations usually differ slightly in exposure and tone.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="manual_scale",
                input_types=["float"],
                type="float",
                default_value=0.5,
                tooltip="align_mode=manual: scale applied to the overlay video.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="manual_center_x",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="align_mode=manual: where the overlay's centre lands in the base frame (px). "
                "0 = centre of the base frame.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="manual_center_y",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="align_mode=manual: where the overlay's centre lands in the base frame (px). "
                "0 = centre of the base frame.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="audio_source",
                type="str",
                default_value="base",
                tooltip="Where the output audio comes from. Both clips share the same audio, so 'base' is "
                "normally right.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=AUDIO_SOURCES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="crf",
                input_types=["int"],
                type="int",
                default_value=16,
                tooltip="x264 quality for the output.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
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
                name="composited_video",
                output_type="VideoUrlArtifact",
                tooltip="The full-body video with the zoomed face blended in.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="alignment_preview",
                output_type="ImageUrlArtifact",
                tooltip="One frame showing the detected heads and the blend boundary — check this before "
                "committing to a long render.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="placement",
                output_type="json",
                tooltip="The computed scale and position, so a good alignment can be recorded or reused.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the overlay result",
            result_details_placeholder="Overlay details will appear here.",
        )

    def validate_before_node_run(self) -> list[Exception] | None:
        if not MODEL_PATH.is_file():
            return [FileNotFoundError(f"Vendored detector weights missing: {MODEL_PATH}")]
        return None

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temp_files: list[Path] = []
        self._saved_files: list[str] = []
        try:
            base_path, tmp = self._artifact_to_local_file(self.parameter_values.get("base_video"), "base_video")
            if tmp:
                temp_files.append(tmp)
            over_path, tmp = self._artifact_to_local_file(self.parameter_values.get("overlay_video"), "overlay_video")
            if tmp:
                temp_files.append(tmp)

            base = self._probe(base_path, "base_video")
            over = self._probe(over_path, "overlay_video")
            self._validate(base, over)

            placement, notes = self._compute_placement(base_path, over_path, base, over)
            self.parameter_output_values["placement"] = placement

            preview_path = Path(tempfile.gettempdir()) / f"overlay_preview_{uuid.uuid4().hex}.png"
            temp_files.append(preview_path)
            self._render_preview(base_path, over_path, placement, preview_path)
            self.parameter_output_values["alignment_preview"] = self._publish_image(preview_path)

            out_path = Path(tempfile.gettempdir()) / f"overlay_{uuid.uuid4().hex}.mp4"
            temp_files.append(out_path)
            self._composite(base_path, over_path, base, over, placement, out_path)

            final_path = self._attach_audio(out_path, base_path, over_path)
            if final_path != out_path:
                temp_files.append(final_path)
            self.parameter_output_values["composited_video"] = self._publish_video(final_path)

            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"Overlaid the zoomed render at scale {placement['scale']:.4f}, "
                    f"top-left ({placement['x']}, {placement['y']}), "
                    f"blend {placement['blend_width']}x{placement['blend_height']} "
                    f"({self.parameter_values.get('edge_shape')}, feather "
                    f"{self.parameter_values.get('feather_px')}px).{notes}\n"
                    f"Check 'alignment_preview' before committing to a long render; nudge with "
                    f"scale_adjust / offset_x / offset_y.{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            for path in temp_files:
                path.unlink(missing_ok=True)

    # -- Validation ----------------------------------------------------------

    def _validate(self, base: dict[str, Any], over: dict[str, Any]) -> None:
        if base["frame_count"] != over["frame_count"]:
            raise ValueError(
                f"base_video has {base['frame_count']} frames but overlay_video has {over['frame_count']} — "
                "they must be the same length (generate both from the same audio). A mismatch would drift "
                "the lipsync out of step partway through."
            )
        if abs(base["frame_rate"] - over["frame_rate"]) > 0.01:
            raise ValueError(
                f"base_video is {base['frame_rate']:.3f} fps but overlay_video is {over['frame_rate']:.3f} fps — "
                "re-render one of them to match."
            )

    # -- Alignment -----------------------------------------------------------

    def _detect_head(self, path: Path, info: dict[str, Any], label: str) -> dict[str, Any]:
        """Median face geometry across sampled frames.

        Alignment uses the eye landmarks, not the bounding box: the box is
        quantised and jitters frame to frame, while inter-ocular distance and
        eye midpoint are stable and are what the eye actually judges alignment by.
        """
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open {label} for face detection.")
        try:
            width, height = info["width"], info["height"]
            detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (width, height), 0.5)
            detector.setInputSize((width, height))
            total = max(1, info["frame_count"])
            samples = []
            for step in range(DETECT_SAMPLE_FRAMES):
                cap.set(cv2.CAP_PROP_POS_FRAMES, min(int(total * (step + 0.5) / DETECT_SAMPLE_FRAMES), total - 1))
                ok, frame = cap.read()
                if not ok:
                    continue
                faces = detector.detect(frame)[1]
                if faces is None or len(faces) == 0:
                    continue
                best = max(faces, key=lambda f: float(f[14]))
                bx, by, bw, bh = (float(best[i]) for i in range(4))
                # Landmarks: 4,5 right eye; 6,7 left eye.
                rex, rey, lex, ley = (float(best[i]) for i in range(4, 8))
                samples.append([bx + bw / 2, by + bh / 2, bw, bh,
                                (rex + lex) / 2, (rey + ley) / 2, math.hypot(bw, bh)])
        finally:
            cap.release()
        if not samples:
            raise RuntimeError(
                f"No face found in {label}. Switch align_mode to manual and set manual_scale / "
                "manual_center_x / manual_center_y, or check that the clip actually shows a face."
            )
        median = np.median(np.array(samples), axis=0)
        return {
            "center": (float(median[0]), float(median[1])),
            "box": (float(median[2]), float(median[3])),
            "anchor": (float(median[4]), float(median[5])),
            "diagonal": float(median[6]),
            "samples": len(samples),
        }

    def _refine_offset(
        self,
        base_path: Path,
        over_path: Path,
        base: dict[str, Any],
        over: dict[str, Any],
        scale: float,
        top_left_x: int,
        top_left_y: int,
    ) -> tuple[float, float, float]:
        """Sub-pixel offset correction by registering the two images directly.

        Face detection gets within a couple of pixels; on a detailed face that is
        still visible. Phase correlation over the overlapping face area closes the
        gap — but only when the two renders actually resemble each other, so the
        caller gates on the returned confidence.
        """
        scaled_w = max(1, int(round(over["width"] * scale)))
        scaled_h = max(1, int(round(over["height"] * scale)))
        window = self._clip_window(
            {"x": top_left_x, "y": top_left_y, "scaled_width": scaled_w, "scaled_height": scaled_h},
            base["width"], base["height"],
        )
        if window is None:
            return 0.0, 0.0, 0.0
        dst_y, dst_x, src_y, src_x = window

        cap_b = cv2.VideoCapture(str(base_path))
        cap_o = cv2.VideoCapture(str(over_path))
        try:
            total = max(1, min(base["frame_count"], over["frame_count"]))
            shifts: list[tuple[float, float, float]] = []
            for step in range(REFINE_SAMPLE_FRAMES):
                index = min(int(total * (step + 0.5) / REFINE_SAMPLE_FRAMES), total - 1)
                cap_b.set(cv2.CAP_PROP_POS_FRAMES, index)
                cap_o.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok_b, frame_b = cap_b.read()
                ok_o, frame_o = cap_o.read()
                if not (ok_b and ok_o):
                    continue
                scaled = cv2.resize(frame_o, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA)
                patch = cv2.cvtColor(scaled[src_y, src_x], cv2.COLOR_BGR2GRAY).astype(np.float32)
                under = cv2.cvtColor(frame_b[dst_y, dst_x], cv2.COLOR_BGR2GRAY).astype(np.float32)
                if patch.shape != under.shape or min(patch.shape) < 16:
                    continue
                # Hann window suppresses edge effects from the rectangular crop.
                hann = cv2.createHanningWindow((patch.shape[1], patch.shape[0]), cv2.CV_32F)
                (dx, dy), response = cv2.phaseCorrelate(under, patch, hann)
                if math.hypot(dx, dy) <= REFINE_MAX_SHIFT_PX:
                    shifts.append((dx, dy, float(response)))
        finally:
            cap_b.release()
            cap_o.release()

        if not shifts:
            return 0.0, 0.0, 0.0
        array = np.array(shifts)
        # phaseCorrelate reports how far `patch` sits from `under`, so invert it
        # to get the correction to apply to the overlay's position.
        return -float(np.median(array[:, 0])), -float(np.median(array[:, 1])), float(np.median(array[:, 2]))

    def _compute_placement(
        self, base_path: Path, over_path: Path, base: dict[str, Any], over: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        mode = self.parameter_values.get("align_mode") or "auto"
        scale_adjust = float(self.parameter_values.get("scale_adjust") or 1.0)
        offset_x = int(self.parameter_values.get("offset_x") or 0)
        offset_y = int(self.parameter_values.get("offset_y") or 0)
        notes = ""

        if mode == "manual":
            scale = float(self.parameter_values.get("manual_scale") or 0.5)
            cx = int(self.parameter_values.get("manual_center_x") or 0) or base["width"] // 2
            cy = int(self.parameter_values.get("manual_center_y") or 0) or base["height"] // 2
            top_left_x = int(round(cx - over["width"] * scale / 2)) + offset_x
            top_left_y = int(round(cy - over["height"] * scale / 2)) + offset_y
            detected = None
        else:
            base_face = self._detect_head(base_path, base, "base_video")
            over_face = self._detect_head(over_path, over, "overlay_video")
            if base_face["diagonal"] < 4 or over_face["diagonal"] < 4:
                raise RuntimeError(
                    "The detected faces are too small to align reliably. Use align_mode=manual."
                )
            # Scale from the face box diagonal, position from the eye midpoint. Measured
            # against a known-scale round trip: the diagonal lands within 0.5% while eye
            # separation was 7.5% out — a longer baseline absorbs detector jitter better —
            # and the eye midpoint is the anchor a viewer judges alignment by.
            scale = (base_face["diagonal"] / over_face["diagonal"]) * scale_adjust
            # Land the overlay's eye midpoint on the base's: eyes are the stable
            # reference, and where a viewer notices misalignment first.
            b_ax, b_ay = base_face["anchor"]
            o_ax, o_ay = over_face["anchor"]
            top_left_x = int(round(b_ax - o_ax * scale)) + offset_x
            top_left_y = int(round(b_ay - o_ay * scale)) + offset_y
            detected = {
                "base_head": [round(base_face["center"][0], 1), round(base_face["center"][1], 1),
                              round(base_face["box"][0], 1), round(base_face["box"][1], 1)],
                "base_anchor": [round(b_ax, 1), round(b_ay, 1)],
                "overlay_anchor": [round(o_ax, 1), round(o_ay, 1)],
                "base_face_diagonal": round(base_face["diagonal"], 2),
                "overlay_face_diagonal": round(over_face["diagonal"], 2),
                "frames_sampled": [base_face["samples"], over_face["samples"]],
            }
            zoom_ratio = over_face["diagonal"] / (base_face["diagonal"] or 1)
            notes = f"\nThe zoomed render's face is {zoom_ratio:.2f}x the size of the base's."
            if zoom_ratio < 1.2:
                notes += (
                    " That is barely a zoom — the quality gain will be small. Crop tighter when generating "
                    "the zoomed source."
                )

        if mode == "auto" and bool(self.parameter_values.get("refine_alignment", True)):
            dx, dy, confidence = self._refine_offset(base_path, over_path, base, over, scale,
                                                     top_left_x, top_left_y)
            if confidence >= REFINE_MIN_CONFIDENCE:
                top_left_x += int(round(dx))
                top_left_y += int(round(dy))
                notes += (
                    f"\nRefined the position by ({dx:+.1f}, {dy:+.1f}) px by direct image registration "
                    f"(confidence {confidence:.2f})."
                )
            else:
                notes += (
                    f"\nSkipped registration refinement — the two renders were too dissimilar to match "
                    f"confidently ({confidence:.2f}). The face-detection estimate stands; nudge with "
                    "offset_x / offset_y if it needs help."
                )

        scaled_w = max(1, int(round(over["width"] * scale)))
        scaled_h = max(1, int(round(over["height"] * scale)))
        coverage = min(1.0, max(0.05, float(self.parameter_values.get("coverage") or 1.0)))
        blend_w = max(2, int(round(scaled_w * coverage)))
        blend_h = max(2, int(round(scaled_h * coverage)))
        placement = {
            "scale": round(scale, 6),
            "x": top_left_x,
            "y": top_left_y,
            "scaled_width": scaled_w,
            "scaled_height": scaled_h,
            "blend_width": blend_w,
            "blend_height": blend_h,
            "base": {"width": base["width"], "height": base["height"], "frame_count": base["frame_count"]},
            "overlay": {"width": over["width"], "height": over["height"]},
            "detected": detected,
        }
        return placement, notes

    def _build_mask(self, placement: dict[str, Any]) -> np.ndarray:
        """Feathered alpha over the blend area, in scaled-overlay coordinates."""
        sw, sh = placement["scaled_width"], placement["scaled_height"]
        bw, bh = placement["blend_width"], placement["blend_height"]
        shape = self.parameter_values.get("edge_shape") or "ellipse"
        feather = max(0, int(self.parameter_values.get("feather_px") or 0))

        mask = np.zeros((sh, sw), dtype=np.uint8)
        cx, cy = sw // 2, sh // 2
        # Inset so the feather has room to fall off inside the blend area.
        inset = max(1, feather // 2)
        half_w = max(1, bw // 2 - inset)
        half_h = max(1, bh // 2 - inset)
        if shape == "ellipse":
            cv2.ellipse(mask, (cx, cy), (half_w, half_h), 0, 0, 360, 255, -1)
        elif shape == "rounded_rect":
            radius = max(2, min(half_w, half_h) // 3)
            x0, y0, x1, y1 = cx - half_w, cy - half_h, cx + half_w, cy + half_h
            cv2.rectangle(mask, (x0 + radius, y0), (x1 - radius, y1), 255, -1)
            cv2.rectangle(mask, (x0, y0 + radius), (x1, y1 - radius), 255, -1)
            for ccx, ccy in ((x0 + radius, y0 + radius), (x1 - radius, y0 + radius),
                             (x0 + radius, y1 - radius), (x1 - radius, y1 - radius)):
                cv2.circle(mask, (ccx, ccy), radius, 255, -1)
        else:
            cv2.rectangle(mask, (cx - half_w, cy - half_h), (cx + half_w, cy + half_h), 255, -1)
        if feather > 0:
            mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(0.5, feather / 3.0))
        return mask.astype(np.float32) / 255.0

    @staticmethod
    def _match_color(insert: np.ndarray, under: np.ndarray, weights: np.ndarray) -> np.ndarray:
        """Match insert's LAB mean/std to the base region it covers, weighted by the blend mask."""
        total = float(weights.sum())
        if total < 1.0:
            return insert
        lab_i = cv2.cvtColor(insert, cv2.COLOR_BGR2LAB).astype(np.float32)
        lab_u = cv2.cvtColor(under, cv2.COLOR_BGR2LAB).astype(np.float32)
        out = lab_i.copy()
        for channel in range(3):
            mi = float((lab_i[..., channel] * weights).sum() / total)
            mu = float((lab_u[..., channel] * weights).sum() / total)
            si = float(np.sqrt(((lab_i[..., channel] - mi) ** 2 * weights).sum() / total)) or 1e-6
            su = float(np.sqrt(((lab_u[..., channel] - mu) ** 2 * weights).sum() / total))
            out[..., channel] = (lab_i[..., channel] - mi) / si * su + mu
        return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)

    # -- Compositing ---------------------------------------------------------

    @staticmethod
    def _clip_window(placement: dict[str, Any], base_w: int, base_h: int) -> tuple[slice, slice, slice, slice] | None:
        x, y = placement["x"], placement["y"]
        sw, sh = placement["scaled_width"], placement["scaled_height"]
        dx0, dy0 = max(0, x), max(0, y)
        dx1, dy1 = min(base_w, x + sw), min(base_h, y + sh)
        if dx1 <= dx0 or dy1 <= dy0:
            return None
        return (
            slice(dy0, dy1),
            slice(dx0, dx1),
            slice(dy0 - y, dy1 - y),
            slice(dx0 - x, dx1 - x),
        )

    def _composite(
        self,
        base_path: Path,
        over_path: Path,
        base: dict[str, Any],
        over: dict[str, Any],
        placement: dict[str, Any],
        out_path: Path,
    ) -> None:
        ffmpeg, _ = _ffmpeg_paths()
        bw, bh = base["width"], base["height"]
        ow, oh = over["width"], over["height"]
        sw, sh = placement["scaled_width"], placement["scaled_height"]
        window = self._clip_window(placement, bw, bh)
        if window is None:
            raise ValueError(
                f"The overlay lands entirely outside the base frame at scale {placement['scale']:.3f}, "
                f"position ({placement['x']}, {placement['y']}). Check offset_x / offset_y."
            )
        dst_y, dst_x, src_y, src_x = window
        mask = self._build_mask(placement)[src_y, src_x][:, :, np.newaxis]
        color_match = bool(self.parameter_values.get("color_match", True))
        interpolation = cv2.INTER_AREA if placement["scale"] < 1.0 else cv2.INTER_LANCZOS4

        base_bytes = bw * bh * 3
        over_bytes = ow * oh * 3
        decoder_base = subprocess.Popen(
            [ffmpeg, "-v", "error", "-i", str(base_path), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        decoder_over = subprocess.Popen(
            [ffmpeg, "-v", "error", "-i", str(over_path), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        encoder = subprocess.Popen(
            [
                ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{bw}x{bh}",
                "-r", f"{base['frame_rate']:.6f}", "-i", "-", "-an", "-c:v", "libx264", "-preset", "slow",
                "-crf", str(int(self.parameter_values.get("crf") or 16)), "-pix_fmt", "yuv420p",
                *_encoder_color_args(base_path), str(out_path),
            ],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        frames = 0
        try:
            while True:
                chunk_b = decoder_base.stdout.read(base_bytes)
                if len(chunk_b) < base_bytes:
                    break
                frame = np.frombuffer(chunk_b, dtype=np.uint8).reshape(bh, bw, 3).copy()
                chunk_o = decoder_over.stdout.read(over_bytes)
                if len(chunk_o) == over_bytes:
                    over_frame = np.frombuffer(chunk_o, dtype=np.uint8).reshape(oh, ow, 3)
                    scaled = cv2.resize(over_frame, (sw, sh), interpolation=interpolation)
                    patch = scaled[src_y, src_x]
                    under = frame[dst_y, dst_x]
                    if color_match:
                        patch = self._match_color(patch, under, mask[:, :, 0])
                    blended = patch.astype(np.float32) * mask + under.astype(np.float32) * (1.0 - mask)
                    frame[dst_y, dst_x] = np.clip(blended, 0, 255).astype(np.uint8)
                encoder.stdin.write(frame.tobytes())
                frames += 1
        finally:
            for proc in (decoder_base, decoder_over):
                proc.stdout.close()
                proc.wait()
            encoder.stdin.close()
            encoder.wait()
        if encoder.returncode != 0 or not out_path.is_file():
            raise RuntimeError("ffmpeg failed while encoding the overlay composite.")
        logger.info("Overlay composited %d frames", frames)

    def _render_preview(
        self, base_path: Path, over_path: Path, placement: dict[str, Any], out_path: Path
    ) -> None:
        cap = cv2.VideoCapture(str(base_path))
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, placement["base"]["frame_count"] // 2))
        ok, frame = cap.read()
        cap.release()
        if not ok:
            raise RuntimeError("Could not read a frame from base_video for the preview.")
        bh, bw = frame.shape[:2]

        cap = cv2.VideoCapture(str(over_path))
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, placement["base"]["frame_count"] // 2))
        ok, over_frame = cap.read()
        cap.release()

        window = self._clip_window(placement, bw, bh)
        if ok and window is not None:
            dst_y, dst_x, src_y, src_x = window
            scaled = cv2.resize(
                over_frame, (placement["scaled_width"], placement["scaled_height"]), interpolation=cv2.INTER_AREA
            )
            mask = self._build_mask(placement)[src_y, src_x][:, :, np.newaxis]
            under = frame[dst_y, dst_x]
            blended = scaled[src_y, src_x].astype(np.float32) * mask + under.astype(np.float32) * (1.0 - mask)
            frame[dst_y, dst_x] = np.clip(blended, 0, 255).astype(np.uint8)

        thickness = max(2, bw // 400)
        cv2.rectangle(
            frame,
            (placement["x"], placement["y"]),
            (placement["x"] + placement["scaled_width"], placement["y"] + placement["scaled_height"]),
            (0, 200, 255), thickness,
        )
        detected = placement.get("detected")
        if detected:
            bcx, bcy, b_w, b_h = detected["base_head"]
            cv2.rectangle(
                frame,
                (int(bcx - b_w / 2), int(bcy - b_h / 2)),
                (int(bcx + b_w / 2), int(bcy + b_h / 2)),
                (0, 255, 0), thickness,
            )
        cv2.imwrite(str(out_path), frame)

    def _attach_audio(self, video_path: Path, base_path: Path, over_path: Path) -> Path:
        source = self.parameter_values.get("audio_source") or "base"
        if source == "none":
            return video_path
        donor = base_path if source == "base" else over_path
        ffmpeg, ffprobe = _ffmpeg_paths()
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(donor)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False,
        )
        if "audio" not in (probe.stdout or ""):
            logger.warning("%s has no audio stream; output will be silent.", source)
            return video_path
        muxed = Path(tempfile.gettempdir()) / f"overlay_muxed_{uuid.uuid4().hex}.mp4"
        result = subprocess.run(
            [ffmpeg, "-y", "-i", str(video_path), "-i", str(donor), "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(muxed)],
            capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
        )
        if result.returncode != 0 or not muxed.is_file():
            raise RuntimeError(f"ffmpeg audio remux failed: {(result.stderr or '')[-400:]}")
        return muxed

    # -- Probing and output --------------------------------------------------

    def _probe(self, path: Path, label: str) -> dict[str, Any]:
        _, ffprobe = _ffmpeg_paths()
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=width,height,avg_frame_rate,nb_read_packets",
             "-of", "default=noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Could not probe {label}: {(result.stderr or '').strip()[:300]}")
        fields: dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
        if not fields.get("width"):
            raise RuntimeError(f"{label} has no video stream.")
        numerator, _, denominator = fields.get("avg_frame_rate", "0/0").partition("/")
        try:
            frame_rate = float(numerator) / float(denominator) if float(denominator or 0) else 25.0
        except (ValueError, ZeroDivisionError):
            frame_rate = 25.0
        return {
            "width": int(fields["width"]),
            "height": int(fields["height"]),
            "frame_rate": frame_rate or 25.0,
            "frame_count": int(fields.get("nb_read_packets") or 0),
        }

    def _publish_video(self, path: Path) -> VideoUrlArtifact:
        data = path.read_bytes()
        filename = f"overlay_{uuid.uuid4().hex[:8]}.mp4"
        saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
        self._save_copy_to_output_directory(data, filename)
        return VideoUrlArtifact(value=saved_url, name=filename)

    def _publish_image(self, path: Path) -> ImageUrlArtifact:
        data = path.read_bytes()
        filename = f"overlay_preview_{uuid.uuid4().hex[:8]}.png"
        saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
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
            logger.warning("Could not save overlay result to %r", directory, exc_info=True)
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

    def _artifact_to_local_file(self, artifact: Any, label: str) -> tuple[Path, Path | None]:
        value = getattr(artifact, "value", artifact)
        if isinstance(value, str) and value and not value.startswith(("http://", "https://", "data:")):
            path = self._resolve_workspace_path(value)
            if path is not None:
                return path, None
        data = self._artifact_to_bytes(artifact, label)
        handle = Path(tempfile.gettempdir()) / f"overlay_{label}_{uuid.uuid4().hex}.mp4"
        handle.write_bytes(data)
        return handle, handle

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
