from __future__ import annotations

import base64
import logging
import re
import subprocess
import tempfile
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

DOWNLOAD_TIMEOUT_SECONDS = 600
FFMPEG_TIMEOUT_SECONDS = 3600
PROBE_TIMEOUT_SECONDS = 300
ALPHA_PIX_FMTS = ("yuva", "rgba", "bgra", "argb", "abgr", "ya")
STILL_MIME_PREFIX = "image/"

MATTE_SOURCES = ["key_auto", "key_manual", "external", "embedded"]
KEY_ALGORITHMS = ["chromakey", "colorkey"]
BACKGROUND_FITS = ["cover", "contain", "stretch"]
AUDIO_SOURCES = ["foreground", "background", "none"]
# Single value on purpose (SPEC 7.9): shipping the enum now means adding
# multiply/holdout later isn't a breaking parameter change.
MATTE_BLEND_MODES = ["replace"]

DEFAULT_KEY_COLOR = "#00B140"
HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

# key_auto corner sampling. Measured on real 4K generated footage 2026-08-05:
# clean backing patches ran 0.5-6.7 max per-channel stddev, so the 12 default is a
# generous ceiling that still rejects nothing clean. Flatness alone is NOT enough
# though - a flat black jacket measured 1.0, flatter than the backing - hence the
# saturation/value gate and the outlier pass below.
KEY_PATCH_PX = 32
KEY_PATCH_INSET = 0.05
MIN_PATCH_SATURATION = 60.0  # HSV S; chroma backing is saturated, black/grey/white subject isn't
MIN_PATCH_VALUE = 40.0  # HSV V; rejects near-black patches, where saturation is meaningless
OUTLIER_DISTANCE = 60.0  # BGR euclidean distance from the median beyond which a patch is dropped


def _ffmpeg_paths() -> tuple[str, str]:
    """(ffmpeg, ffprobe) executables; static_ffmpeg downloads them on first use."""
    import static_ffmpeg.run

    return static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()


def _parse_hex_color(value: str, label: str) -> str:
    """'#00B140' -> '0x00B140'. ffmpeg wants 0x-prefixed hex, not '#'."""
    match = HEX_COLOR_RE.match((value or "").strip())
    if not match:
        raise ValueError(f"{label} must be a 6-digit hex colour like #00B140 (got {value!r}).")
    return f"0x{match.group(1).upper()}"


def _sniff_mime(data: bytes, fallback: str) -> str:
    """Detect the content type from magic bytes; artifact metadata is unreliable across sources."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return fallback


class CompositeOverBackground(SuccessFailureNode):
    """Composite a subject video over a background still or video.

    Alpha comes from one of four sources (matte_source): a key colour sampled
    from the footage, a supplied key colour, an external black-and-white matte,
    or the foreground's own alpha channel.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/composite",
            "description": "Composite a subject video over a background image or video, keying or matting as needed.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="foreground_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="The subject video — keyed, matted, or already carrying alpha.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="background",
                input_types=["ImageArtifact", "ImageUrlArtifact", "VideoUrlArtifact"],
                type="ImageArtifact",
                tooltip="Background still plate or moving background video. Ignored when output_alpha is on.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_source",
                type="str",
                default_value="key_auto",
                tooltip="Where alpha comes from. key_auto samples the key colour from the footage; "
                "key_manual uses key_color; external uses matte_video; embedded uses the foreground's "
                "own alpha channel (webm/ProRes 4444).",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=MATTE_SOURCES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="key_color",
                input_types=["str"],
                type="str",
                default_value=DEFAULT_KEY_COLOR,
                tooltip="Key colour for key_manual; ignored otherwise. #00B140 is mid-saturation digital green — "
                "#00FF00 clips and spills hard on hair.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="key_algorithm",
                type="str",
                default_value="chromakey",
                tooltip="chromakey keys on UV and ignores luma, so shading and lighting drift cost nothing. "
                "colorkey is RGB — for flat graphics.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=KEY_ALGORITHMS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="similarity",
                input_types=["float"],
                type="float",
                default_value=0.10,
                tooltip="Key tolerance — the usable window is narrow and the failure is abrupt. Measured on "
                "real generated footage: 0.10-0.12 keeps the subject and clears the backing, while 0.15 "
                "erased 95% of the subject and 0.25 erased all of it. Green fringe left over? Nudge up "
                "toward 0.12. Subject vanishing? You have gone over — back off. Watch the 'matte' output "
                "while tuning.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="blend",
                input_types=["float"],
                type="float",
                default_value=0.10,
                tooltip="Key edge softness.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_video",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                tooltip="External matte (matte_source=external). Luma is read as alpha: WHITE = OPAQUE, "
                "BLACK = TRANSPARENT.",
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="invert_matte",
                input_types=["bool"],
                type="bool",
                default_value=False,
                tooltip="Flip matte polarity, for sources where black means opaque.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="despill",
                input_types=["bool"],
                type="bool",
                default_value=True,
                tooltip="Remove green spill from the subject. Independent of matte_source — a green plate "
                "spills whether or not the matte came from elsewhere.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="despill_amount",
                input_types=["float"],
                type="float",
                default_value=0.5,
                tooltip="Advanced: despill strength.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_erode_px",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Advanced: erode the matte before feathering, so the blend edge sits inside the subject.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_feather_px",
                input_types=["int"],
                type="int",
                default_value=0,
                tooltip="Advanced: blur the matte edge by this many pixels.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte_blend_mode",
                type="str",
                default_value="replace",
                tooltip="How an external matte combines with the keyed alpha. Only 'replace' for now.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=MATTE_BLEND_MODES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="key_sample_frames",
                input_types=["int"],
                type="int",
                default_value=5,
                tooltip="Advanced (key_auto): how many frames to sample evenly across the clip.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="key_sample_tolerance",
                input_types=["float"],
                type="float",
                default_value=12.0,
                tooltip="Advanced (key_auto): reject a corner patch whose per-channel stddev exceeds this — "
                "that's subject or gradient intruding, not clean backing.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="background_fit",
                type="str",
                default_value="cover",
                tooltip="How the background conforms to the foreground's dimensions. The foreground is never "
                "resampled.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=BACKGROUND_FITS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="audio_source",
                type="str",
                default_value="foreground",
                tooltip="Where the output audio comes from.",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=AUDIO_SOURCES)},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_alpha",
                type="bool",
                default_value=False,
                tooltip="Skip compositing and emit a VP9 webm with a real alpha channel — makes this a "
                "standalone keyer. background is ignored.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="crf",
                input_types=["int"],
                type="int",
                default_value=16,
                tooltip="x264 (or VP9) quality. Lower is better quality and larger.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Optional folder to also save outputs into, e.g. {project_dir}/outputs. "
                "Leave empty to skip saving file copies.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="composited_video",
                output_type="VideoUrlArtifact",
                tooltip="The finished composite (or the alpha webm when output_alpha is on).",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="matte",
                output_type="VideoUrlArtifact",
                tooltip="The alpha as black-and-white video. Wire this to a Display Video — tuning "
                "similarity/blend by eyeballing the comp is miserable. Also the file you fix in Resolve "
                "and feed back as matte_video.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="detected_key_color",
                output_type="str",
                tooltip="What key_auto measured. Paste into key_color + key_manual for a repeatable run.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the composite result",
            result_details_placeholder="Composite details will appear here.",
        )

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        temp_files: list[Path] = []
        try:
            matte_source = self.parameter_values.get("matte_source") or "key_auto"

            fg_path, tmp = self._artifact_to_local_file(self.parameter_values.get("foreground_video"), "foreground")
            if tmp:
                temp_files.append(tmp)
            foreground = self._probe(fg_path, "foreground_video")

            matte_path_in: Path | None = None
            if matte_source == "external":
                matte_path_in, tmp = self._artifact_to_local_file(self.parameter_values.get("matte_video"), "matte")
                if tmp:
                    temp_files.append(tmp)
                self._validate_external_matte(matte_path_in, foreground)
            elif matte_source == "embedded":
                self._validate_embedded_alpha(foreground)

            output_alpha = bool(self.parameter_values.get("output_alpha", False))
            bg_path: Path | None = None
            bg_is_still = False
            if not output_alpha:
                bg_path, tmp, bg_is_still = self._resolve_background(foreground)
                if tmp:
                    temp_files.append(tmp)

            if matte_source == "key_auto":
                key_color, sample_note = self._sample_key_color(fg_path, foreground)
                self.parameter_output_values["detected_key_color"] = f"#{key_color[2:]}"
            else:
                key_color = _parse_hex_color(self.parameter_values.get("key_color") or DEFAULT_KEY_COLOR, "key_color")
                sample_note = ""
                self.parameter_output_values["detected_key_color"] = ""

            comp_suffix = ".webm" if output_alpha else ".mp4"
            comp_path = Path(tempfile.gettempdir()) / f"composite_{uuid.uuid4().hex}{comp_suffix}"
            matte_path = Path(tempfile.gettempdir()) / f"composite_matte_{uuid.uuid4().hex}.mp4"
            temp_files += [comp_path, matte_path]

            self._run_composite(
                foreground=foreground,
                fg_path=fg_path,
                bg_path=bg_path,
                bg_is_still=bg_is_still,
                key_color=key_color,
                comp_path=comp_path,
                matte_path=matte_path,
                mode=matte_source,
                matte_in=matte_path_in,
            )

            comp_artifact = self._publish(comp_path, "composited")
            matte_artifact = self._publish(matte_path, "matte")
            self.parameter_output_values["composited_video"] = comp_artifact
            self.parameter_output_values["matte"] = matte_artifact

            if matte_source in ("key_auto", "key_manual"):
                how = (
                    f"Keyed #{key_color[2:]} (similarity {self.parameter_values.get('similarity')}, "
                    f"blend {self.parameter_values.get('blend')})"
                )
            elif matte_source == "external":
                how = "Used the supplied matte_video as alpha"
                if bool(self.parameter_values.get("invert_matte", False)):
                    how += " (inverted)"
            else:
                how = f"Used the foreground's embedded alpha ({foreground['pix_fmt']})"
            target = "alpha webm (no background)" if output_alpha else (
                f"{'still' if bg_is_still else 'video'} background"
            )
            saved_note = "\nSaved files:\n" + "\n".join(self._saved_files) if self._saved_files else ""
            self._set_status_results(
                was_successful=True,
                result_details=(
                    f"{how} over {target}; "
                    f"{foreground['width']}x{foreground['height']} @ {foreground['frame_rate']:.3f} fps, "
                    f"{foreground['frame_count']} frames.{sample_note}\n"
                    f"Wire the 'matte' output to a Display Video to check the edge.{saved_note}"
                ),
            )
        except Exception as e:
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)
        finally:
            for path in temp_files:
                path.unlink(missing_ok=True)

    # -- Probing and inputs --------------------------------------------------

    def _probe(self, path: Path, label: str) -> dict[str, Any]:
        _, ffprobe = _ffmpeg_paths()
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                "-show_entries", "stream=width,height,avg_frame_rate,nb_read_packets,pix_fmt,duration",
                "-show_entries", "stream_tags=alpha_mode",
                "-of", "default=noprint_wrappers=1", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Could not probe {label}: {(result.stderr or '').strip()[:300]}")
        fields: dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
        if not fields.get("width"):
            raise RuntimeError(f"{label} has no video stream.")

        rate = fields.get("avg_frame_rate", "0/0")
        numerator, _, denominator = rate.partition("/")
        try:
            frame_rate = float(numerator) / float(denominator) if float(denominator or 0) else 0.0
        except (ValueError, ZeroDivisionError):
            frame_rate = 0.0

        audio = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        try:
            duration = float(fields.get("duration") or 0.0)
        except ValueError:
            duration = 0.0
        return {
            "width": int(fields["width"]),
            "height": int(fields["height"]),
            "frame_rate": frame_rate or 25.0,
            "frame_count": int(fields.get("nb_read_packets") or 0),
            "pix_fmt": fields.get("pix_fmt", ""),
            # WebM/VP9 keeps alpha in a side-data layer, so pix_fmt still reads
            # yuv420p and this tag is the only signal that alpha is present.
            "alpha_mode": (fields.get("TAG:alpha_mode") or fields.get("alpha_mode") or "").strip(),
            "duration": duration,
            "has_audio": "audio" in (audio.stdout or ""),
            "path": path,
        }

    def _resolve_background(self, foreground: dict[str, Any]) -> tuple[Path, Path | None, bool]:
        """Returns (path, temp_to_delete, is_still). Fails if a video background is shorter than the foreground."""
        artifact = self.parameter_values.get("background")
        if artifact is None:
            raise ValueError("No background input connected (required unless output_alpha is on).")
        data = self._artifact_to_bytes(artifact, "background")
        mime = _sniff_mime(data, "video/mp4")
        is_still = mime.startswith(STILL_MIME_PREFIX)
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/webm": ".webm"}.get(
            mime, ".mp4"
        )
        handle = Path(tempfile.gettempdir()) / f"composite_bg_{uuid.uuid4().hex}{suffix}"
        handle.write_bytes(data)

        if not is_still:
            background = self._probe(handle, "background")
            fg_duration = foreground["duration"] or (
                foreground["frame_count"] / foreground["frame_rate"] if foreground["frame_rate"] else 0.0
            )
            if background["duration"] and fg_duration and background["duration"] + 0.05 < fg_duration:
                handle.unlink(missing_ok=True)
                raise ValueError(
                    f"Background video is {background['duration']:.2f}s but the foreground is {fg_duration:.2f}s — "
                    "the performance would be truncated to fit the plate. Use a longer background."
                )
        return handle, handle, is_still

    def _validate_external_matte(self, matte_path: Path, foreground: dict[str, Any]) -> None:
        """SPEC 7.5: a matte off by frames is subtly wrong everywhere; fail rather than let shortest=1 hide it."""
        matte = self._probe(matte_path, "matte_video")
        if matte["frame_count"] != foreground["frame_count"]:
            raise ValueError(
                f"matte_video has {matte['frame_count']} frames but the foreground has "
                f"{foreground['frame_count']} — the matte would drift out of sync. Re-export the matte "
                "over the same frame range."
            )
        if abs(matte["frame_rate"] - foreground["frame_rate"]) > 0.01:
            raise ValueError(
                f"matte_video is {matte['frame_rate']:.3f} fps but the foreground is "
                f"{foreground['frame_rate']:.3f} fps — re-export the matte at the foreground's rate."
            )

    def _validate_embedded_alpha(self, foreground: dict[str, Any]) -> None:
        pix_fmt = (foreground["pix_fmt"] or "").lower()
        # Two ways alpha shows up: in the pixel format (ProRes 4444, rgba...) or,
        # for WebM/VP9, only as the alpha_mode tag with pix_fmt still reading yuv420p.
        if foreground.get("alpha_mode") == "1":
            return
        if not pix_fmt.startswith(ALPHA_PIX_FMTS):
            raise ValueError(
                f"matte_source=embedded needs a foreground that carries alpha, but its pixel format is "
                f"'{pix_fmt or 'unknown'}' (no alpha channel). This is what you get when a provider ignores "
                "a webm/alpha request and returns plain mp4. Use key_auto/key_manual instead, or re-export "
                "the foreground as webm (yuva420p) or ProRes 4444."
            )

    # -- key_auto sampling ---------------------------------------------------

    def _sample_key_color(self, fg_path: Path, foreground: dict[str, Any]) -> tuple[str, str]:
        """Sample the key colour from the footage's corners.

        Three gates, because flatness alone is not enough: on real footage a flat
        black jacket is *flatter* than the backing, so a subject in a corner would
        otherwise be accepted and poison the median.
        """
        import cv2  # local import: keeps module import cheap for the non-key modes
        import numpy as np

        frames_to_sample = max(1, int(self.parameter_values.get("key_sample_frames") or 5))
        tolerance = float(self.parameter_values.get("key_sample_tolerance") or 12.0)

        cap = cv2.VideoCapture(str(fg_path))
        if not cap.isOpened():
            raise RuntimeError("key_auto could not open the foreground video to sample the key colour.")
        try:
            width, height = foreground["width"], foreground["height"]
            total = foreground["frame_count"] or int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            inset_x, inset_y = int(width * KEY_PATCH_INSET), int(height * KEY_PATCH_INSET)
            patch = min(KEY_PATCH_PX, max(4, min(width, height) // 8))
            corners = [
                (inset_x, inset_y),
                (max(0, width - inset_x - patch), inset_y),
                (inset_x, max(0, height - inset_y - patch)),
                (max(0, width - inset_x - patch), max(0, height - inset_y - patch)),
            ]

            accepted: list[np.ndarray] = []
            rejected_flat = rejected_dull = 0
            for step in range(frames_to_sample):
                index = int(total * (step + 0.5) / frames_to_sample)
                cap.set(cv2.CAP_PROP_POS_FRAMES, min(index, max(0, total - 1)))
                ok, frame = cap.read()
                if not ok:
                    continue
                for x, y in corners:
                    region = frame[y : y + patch, x : x + patch]
                    if region.size == 0:
                        continue
                    flat = region.reshape(-1, 3).astype(np.float64)
                    if flat.std(axis=0).max() > tolerance:
                        rejected_flat += 1  # subject edge or a gradient intruding
                        continue
                    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float64).mean(axis=0)
                    if hsv[1] < MIN_PATCH_SATURATION or hsv[2] < MIN_PATCH_VALUE:
                        rejected_dull += 1  # flat but unsaturated: black/grey/white subject, not chroma backing
                        continue
                    accepted.append(flat.mean(axis=0))
        finally:
            cap.release()

        total_patches = frames_to_sample * len(corners)
        if not accepted:
            raise RuntimeError(
                f"key_auto found no clean backing in {total_patches} corner patches "
                f"({rejected_flat} too varied, {rejected_dull} not saturated enough to be chroma backing). "
                "The subject may fill the frame, or the backing may not be a chroma colour. "
                "Switch matte_source to key_manual and set key_color yourself."
            )

        stack = np.array(accepted)
        median = np.median(stack, axis=0)
        # Outlier pass: a flat, saturated patch that disagrees with the rest is
        # something else entirely (a coloured prop, a lit gel), not the backing.
        distances = np.linalg.norm(stack - median, axis=1)
        keep = stack[distances <= OUTLIER_DISTANCE]
        rejected_outlier = len(stack) - len(keep)
        if len(keep):
            median = np.median(keep, axis=0)

        blue, green, red = (int(round(c)) for c in median)
        hex_color = f"0x{red:02X}{green:02X}{blue:02X}"
        spread = float(np.linalg.norm(stack.max(axis=0) - stack.min(axis=0))) if len(stack) > 1 else 0.0
        note = (
            f"\nkey_auto sampled #{red:02X}{green:02X}{blue:02X} from {len(keep) or len(stack)}/{total_patches} "
            f"corner patches (rejected: {rejected_flat} varied, {rejected_dull} unsaturated, "
            f"{rejected_outlier} outlying; corner-to-corner spread {spread:.0f})."
        )
        if spread > 40:
            note += (
                "\nNOTE: the backing varies a lot across the frame (lighting falloff or a shadow). "
                "If edges look uneven, raise 'similarity'."
            )
        return hex_color, note

    # -- Filtergraph ---------------------------------------------------------

    def _background_chain(self, width: int, height: int, frame_rate: float, is_still: bool) -> str:
        fit = self.parameter_values.get("background_fit") or "cover"
        if fit == "stretch":
            scale = f"scale={width}:{height}"
        elif fit == "contain":
            scale = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
        else:
            scale = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
        # Conform a video background's rate to the foreground's; a still is fed at
        # the right rate by -framerate on its input.
        rate = "" if is_still else f",fps={frame_rate:.6f}"
        return f"[0:v]{scale}{rate},setsar=1,format=yuv420p[bg]"

    def _matte_processing_chain(self) -> str:
        """erode -> feather on the extracted alpha. ffmpeg's erosion is a fixed 3x3, so chain it per pixel."""
        erode = max(0, int(self.parameter_values.get("matte_erode_px") or 0))
        feather = max(0, int(self.parameter_values.get("matte_feather_px") or 0))
        steps = ["erosion"] * min(erode, 32)
        if feather > 0:
            steps.append(f"gblur=sigma={max(0.5, feather / 3.0):.3f}")
        return ("," + ",".join(steps)) if steps else ""

    def _despill_step(self) -> str | None:
        if not bool(self.parameter_values.get("despill", True)):
            return None
        amount = float(self.parameter_values.get("despill_amount") or 0.5)
        return f"despill=type=green:mix={amount}:expand=0"

    def _alpha_source_parts(self, mode: str, key_color: str, width: int, height: int) -> list[str]:
        """Everything up to [keyed] — a foreground stream carrying alpha. Differs per matte_source."""
        despill = self._despill_step()
        if mode == "external":
            # The matte's own luma becomes alpha. Scaled to the foreground; a
            # resolution mismatch is fine (SPEC 7.5), a frame-count one is not.
            matte_chain = ["[2:v]format=gray"]
            if bool(self.parameter_values.get("invert_matte", False)):
                matte_chain.append("negate")
            matte_chain.append(f"scale={width}:{height}")
            fg_chain = ["[1:v]"] if not despill else [f"[1:v]{despill}"]
            return [
                ",".join(matte_chain) + "[m0]",
                (",".join(fg_chain) + "[fgbase]") if despill else "[1:v]null[fgbase]",
                "[fgbase][m0]alphamerge[keyed]",
            ]
        if mode == "embedded":
            # Already carries alpha; format=yuva420p keeps it through despill.
            chain = ["[1:v]format=yuva420p"]
            if despill:
                chain.append(despill)
            if bool(self.parameter_values.get("invert_matte", False)):
                # Flip the existing alpha without disturbing colour.
                return [
                    ",".join(chain) + "[emb]",
                    "[emb]split[e_a][e_b]",
                    "[e_a]format=yuva420p,alphaextract,negate[e_m]",
                    "[e_b][e_m]alphamerge[keyed]",
                ]
            return [",".join(chain) + "[keyed]"]

        similarity = float(self.parameter_values.get("similarity") or 0.25)
        blend = float(self.parameter_values.get("blend") or 0.10)
        algorithm = self.parameter_values.get("key_algorithm") or "chromakey"
        # invert_matte is deliberately not applied to a key result: inverting a key
        # just means keying the subject instead of the backing, which is what
        # changing key_color does. It applies to external and embedded sources.
        chain = [f"[1:v]{algorithm}={key_color}:{similarity}:{blend}"]
        if despill:
            chain.append(despill)
        return [",".join(chain) + "[keyed]"]

    def _key_filtergraph(
        self, foreground: dict[str, Any], key_color: str, *, is_still: bool, mode: str = "key_manual"
    ) -> str:
        width, height = foreground["width"], foreground["height"]
        output_alpha = bool(self.parameter_values.get("output_alpha", False))

        parts = self._alpha_source_parts(mode, key_color, width, height)

        matte_ops = self._matte_processing_chain()
        if matte_ops:
            # Rebuild alpha only when erode/feather actually do something.
            parts.append("[keyed]split[k_a][k_b]")
            parts.append(f"[k_a]format=yuva420p,alphaextract{matte_ops}[m]")
            parts.append("[m]split[m_merge][m_out]")
            parts.append("[k_b][m_merge]alphamerge[fg]")
            matte_label = "[m_out]"
        else:
            parts.append("[keyed]split[fg][fg_m]")
            parts.append("[fg_m]format=yuva420p,alphaextract[m_out]")
            matte_label = "[m_out]"

        if output_alpha:
            # Pin the format: without it the encoder negotiates down to yuv420p and
            # silently drops the alpha this mode exists to produce.
            parts.append("[fg]format=yuva420p[v]")
        else:
            parts.append(self._background_chain(width, height, foreground["frame_rate"], is_still))
            parts.append("[bg][fg]overlay=shortest=1:format=auto[v]")
        parts.append(f"{matte_label}format=gray[matteout]")
        return ";".join(parts)

    def _run_composite(
        self,
        *,
        foreground: dict[str, Any],
        fg_path: Path,
        bg_path: Path | None,
        bg_is_still: bool,
        key_color: str,
        comp_path: Path,
        matte_path: Path,
        mode: str = "key_manual",
        matte_in: Path | None = None,
    ) -> None:
        ffmpeg, _ = _ffmpeg_paths()
        crf = int(self.parameter_values.get("crf") or 16)
        output_alpha = bool(self.parameter_values.get("output_alpha", False))
        audio_source = self.parameter_values.get("audio_source") or "foreground"

        command = [ffmpeg, "-y"]
        if not output_alpha and bg_path is not None:
            if bg_is_still:
                command += ["-loop", "1", "-framerate", f"{foreground['frame_rate']:.6f}"]
            command += ["-i", str(bg_path)]
        else:
            # Input 0 must exist so the graph's indices stay stable; a 1-frame null
            # source is cheap and never reaches the output in output_alpha mode.
            command += ["-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1"]
        # ffmpeg's native VP9 decoder silently drops the alpha layer; only the
        # libvpx-vp9 decoder exposes it, so an alpha webm must be decoded with it
        # or embedded mode composites an opaque rectangle.
        if mode == "embedded" and foreground.get("alpha_mode") == "1":
            command += ["-c:v", "libvpx-vp9"]
        command += ["-i", str(fg_path)]
        if mode == "external":
            if matte_in is None:
                raise ValueError("matte_source=external requires a matte_video input.")
            command += ["-i", str(matte_in)]

        graph = self._key_filtergraph(foreground, key_color, is_still=bg_is_still, mode=mode)
        command += ["-filter_complex", graph]

        command += ["-map", "[v]"]
        if audio_source == "foreground":
            command += ["-map", "1:a?"]
        elif audio_source == "background" and not output_alpha:
            command += ["-map", "0:a?"]
        if output_alpha:
            # VP9 is the only widely-supported encoder here that carries alpha.
            command += ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", str(crf), "-b:v", "0"]
        else:
            command += ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p"]
        if audio_source != "none":
            command += ["-c:a", "aac", "-b:a", "192k"]
        if bg_is_still and not output_alpha:
            command += ["-shortest"]
        command += [str(comp_path)]

        # Second output of the same invocation: one decode, one key, two encodes.
        command += [
            "-map", "[matteout]", "-an", "-c:v", "libx264", "-preset", "slow",
            "-crf", str(crf), "-pix_fmt", "yuv420p", str(matte_path),
        ]

        logger.info("Composite ffmpeg filtergraph: %s", graph)
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False
        )
        if result.returncode != 0 or not comp_path.is_file() or not matte_path.is_file():
            raise RuntimeError(f"ffmpeg composite failed: {(result.stderr or '').strip()[-600:]}")
        self._comp_path = comp_path

    # -- Output --------------------------------------------------------------

    def _publish(self, path: Path, label: str) -> VideoUrlArtifact:
        if not hasattr(self, "_saved_files"):
            self._saved_files = []
        data = path.read_bytes()
        filename = f"composite_{label}_{uuid.uuid4().hex[:8]}{path.suffix}"
        try:
            saved_url = GriptapeNodes.StaticFilesManager().save_static_file(data, filename)
        except Exception:
            logger.warning("Could not save %s to static files", filename, exc_info=True)
            raise
        self._save_copy_to_output_directory(data, filename)
        return VideoUrlArtifact(value=saved_url, name=filename)

    def _save_copy_to_output_directory(self, data: bytes, filename: str) -> None:
        """Optionally write the output into the user-chosen folder; failures are reported, not fatal."""
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
            logger.warning("Could not save output to %r", directory, exc_info=True)
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

    def _artifact_to_local_file(self, artifact: Any, label: str) -> tuple[Path, Path | None]:
        """Resolve an artifact to a local file path; second element is a temp file to delete, if any."""
        value = getattr(artifact, "value", artifact)
        if isinstance(value, str) and value and not value.startswith(("http://", "https://", "data:")):
            path = self._resolve_workspace_path(value)
            if path is not None:
                return path, None
        data = self._artifact_to_bytes(artifact, label)
        suffix = ".webm" if _sniff_mime(data, "video/mp4") == "video/webm" else ".mp4"
        handle = Path(tempfile.gettempdir()) / f"composite_{label}_{uuid.uuid4().hex}{suffix}"
        handle.write_bytes(data)
        return handle, handle

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
