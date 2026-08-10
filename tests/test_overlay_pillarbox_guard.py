"""The Overlay Zoomed Video geometry guard, and the Shot Settings node feeding it.

The guard exists because a generator left on aspect_ratio "auto" returned 16:9
for a portrait input, and the pillarbox bars were then blended over the plate as
a dark rectangle around the face. Alignment computed correctly throughout, which
is what made it worth failing loudly over.

The false-positive cases matter as much as the true one: the first version used
max() across opposite edges and refused a perfectly good render whose black
throne filled the bottom of frame.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from conftest import load_node_module
from griptape_nodes.exe_types.core_types import ParameterMode

OVERLAY = load_node_module("composite/overlay_zoomed_video.py", "hr_test_overlay")
SHOT = load_node_module("config/shot_settings.py", "hr_test_shot")
HEYGEN = load_node_module("heygen/avatar_video.py", "hr_test_heygen")

FRAMES = 12
SHARED = ("aspect_ratio", "resolution", "expressiveness", "upscale_long_edge", "output_directory")


def _encode(ffmpeg: str, path: Path, frames: list[np.ndarray], width: int, height: int) -> Path:
    raw = path.with_suffix(".raw")
    raw.write_bytes(b"".join(f.tobytes() for f in frames))
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{width}x{height}", "-r", "25", "-i", str(raw),
         "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )
    raw.unlink(missing_ok=True)
    return path


@pytest.fixture(scope="module")
def clips(ffmpeg: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("clips")
    rng = np.random.default_rng(0)

    def subject(frame: np.ndarray, x0: int, x1: int, height: int) -> None:
        frame[:, x0:x1] = (40, 90, 60)
        cv2.circle(frame, ((x0 + x1) // 2, height // 3), min(height, x1 - x0) // 5, (180, 170, 200), -1)

    # A clean portrait render.
    clean = [np.zeros((1920, 1080, 3), np.uint8) for _ in range(FRAMES)]
    for f in clean:
        subject(f, 0, 1080, 1920)

    # Portrait content pillarboxed into a 16:9 frame — what "auto" produced.
    pillar = [np.zeros((1080, 1920, 3), np.uint8) for _ in range(FRAMES)]
    for f in pillar:
        subject(f, 596, 1920 - 596, 1080)

    # Genuinely dark content everywhere: a night exterior, not bars.
    dark = [np.zeros((1920, 1080, 3), np.uint8) for _ in range(FRAMES)]
    for f in dark:
        f[:] = (8, 8, 8) + rng.integers(0, 4, (1920, 1080, 3), dtype=np.uint8)

    # The real regression: a black throne filling the bottom of an otherwise fine
    # portrait frame. Measures 0px top / 71px bottom.
    chair = [np.zeros((1920, 1080, 3), np.uint8) for _ in range(FRAMES)]
    for f in chair:
        subject(f, 0, 1080, 1920)
        f[1920 - 71:] = rng.integers(0, 3, (71, 1080, 3), dtype=np.uint8)

    return {
        "clean": _encode(ffmpeg, out / "clean.mp4", clean, 1080, 1920),
        "pillar": _encode(ffmpeg, out / "pillar.mp4", pillar, 1920, 1080),
        "dark": _encode(ffmpeg, out / "dark.mp4", dark, 1080, 1920),
        "chair": _encode(ffmpeg, out / "chair.mp4", chair, 1080, 1920),
    }


@pytest.fixture
def node() -> Any:
    return OVERLAY.OverlayZoomedVideo(name="Overlay")


def _validate(node: Any, base: Path, overlay: Path) -> None:
    node._validate(base, overlay, node._probe(base, "base"), node._probe(overlay, "over"))


def test_matching_portrait_overlay_is_accepted(node: Any, clips: dict[str, Path]) -> None:
    _validate(node, clips["clean"], clips["clean"])


def test_pillarboxed_overlay_is_refused(node: Any, clips: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="pillarboxed") as excinfo:
        _validate(node, clips["clean"], clips["pillar"])
    message = str(excinfo.value)
    assert "596px left" in message, message
    assert "aspect ratio" in message, message


def test_uniformly_dark_content_is_not_mistaken_for_bars(node: Any, clips: dict[str, Path]) -> None:
    """Without the centre-brightness check this measured bars wider than the frame itself."""
    _validate(node, clips["clean"], clips["dark"])


def test_dark_content_on_one_edge_only_is_accepted(node: Any, clips: dict[str, Path]) -> None:
    """Regression: a black throne at the bottom of frame, 0px top / 71px bottom.

    Padding is always centred, so bars come in pairs. The first version took the
    max across opposite edges and blocked a good Ozzy render on this exact shape.
    """
    bars = node._measure_bars(clips["chair"], node._probe(clips["chair"], "over"))
    left, right, top, bottom, _centre = bars
    assert (left, right, top) == (0, 0, 0), bars
    assert bottom > 0, "the fixture should have a genuinely dark bottom edge"
    _validate(node, clips["clean"], clips["chair"])


def test_frame_count_mismatch_still_fails(node: Any, clips: dict[str, Path]) -> None:
    base = node._probe(clips["clean"], "base")
    over = dict(node._probe(clips["clean"], "over"))
    over["frame_count"] = base["frame_count"] + 5
    with pytest.raises(ValueError, match="frames"):
        node._validate(clips["clean"], clips["clean"], base, over)


# -- Shot Settings ----------------------------------------------------------


def test_shot_settings_publishes_every_shared_value() -> None:
    node = SHOT.ShotSettings(name="Shot Settings")
    node.parameter_values.update({"aspect_ratio": "9:16", "resolution": "1080p"})
    node.process()
    for name in SHARED:
        assert name in node.parameter_output_values, name
    assert node.parameter_output_values["aspect_ratio"] == "9:16"


def test_shot_settings_publishes_defaults_when_untouched() -> None:
    node = SHOT.ShotSettings(name="fresh")
    node.process()
    assert node.parameter_output_values["upscale_long_edge"] == 1920
    assert node.parameter_output_values["aspect_ratio"] == "9:16"


def test_shot_settings_does_not_offer_auto() -> None:
    """A shot's aspect is a decision; 'auto' returned 16:9 for a portrait input."""
    assert "auto" not in SHOT.ASPECT_RATIOS


@pytest.mark.parametrize("name", ["aspect_ratio", "resolution", "expressiveness"])
def test_heygen_shot_params_accept_connections(name: str) -> None:
    """They were PROPERTY-only, so nothing could drive them from one place."""
    node = HEYGEN.HeyGenAvatarVideo(name="Lipsync")
    param = next(p for p in node.parameters if p.name == name)
    assert ParameterMode.INPUT in param.allowed_modes


def test_heygen_aspect_default_is_explicit_but_auto_still_loads() -> None:
    node = HEYGEN.HeyGenAvatarVideo(name="Lipsync")
    param = next(p for p in node.parameters if p.name == "aspect_ratio")
    assert param.default_value == "9:16"
    assert "auto" in HEYGEN.ASPECT_RATIOS, "saved workflows holding 'auto' must still load"
