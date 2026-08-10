"""TopazImageUpscale request building and response handling.

Every case here comes from a real failure or a real contract detail, not from
imagination — the Topaz Image API disagreed with its own docs twice.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from conftest import load_node_module

MODULE = load_node_module("topaz/image_upscale.py", "hr_test_topaz_image")

# The real Ozzy head crop: 776x1380, which is 9:16 to within a rounding error.
OZZY_W, OZZY_H = 776, 1380
PROCESS_ID = "019fe259-9f05-716d-aca9-13f816cf8c8e"


@pytest.fixture
def node() -> Any:
    instance = MODULE.TopazImageUpscale(name="Upscale Zoomed Face")
    instance.parameter_values.update(
        {
            "model": "High Fidelity V2",
            "output_long_edge": 1920,
            "face_enhancement": True,
            "face_enhancement_strength": 0.8,
            "face_enhancement_creativity": 0.0,
            "subject_detection": "All",
            "output_format": "png",
            "denoise": -1.0,
            "sharpen": -1.0,
            "fix_compression": -1.0,
            "strength": -1.0,
        }
    )
    return instance


# -- Defaults ---------------------------------------------------------------


def test_creativity_defaults_to_zero_for_likeness_safety(node: Any) -> None:
    """Above 0 Topaz invents facial detail, which drifts a real person's likeness."""
    params = {p.name: p for p in node.parameters}
    assert params["face_enhancement_creativity"].default_value == 0.0


def test_output_defaults_to_lossless_png(node: Any) -> None:
    """The result feeds another generative pass, so it must not be recompressed."""
    params = {p.name: p for p in node.parameters}
    assert params["output_format"].default_value == "png"


# -- Sizing: one dimension only ---------------------------------------------


def test_portrait_sends_only_height(node: Any) -> None:
    """Sending both dimensions letterboxes when the aspect differs even slightly."""
    fields = node._build_fields(OZZY_W, OZZY_H)
    assert fields["output_height"] == "1920"
    assert "output_width" not in fields


def test_landscape_sends_only_width(node: Any) -> None:
    fields = node._build_fields(1920, 1080)
    assert fields["output_width"] == "1920"
    assert "output_height" not in fields


def test_zero_long_edge_sends_no_dimension(node: Any) -> None:
    node.parameter_values["output_long_edge"] = 0
    fields = node._build_fields(OZZY_W, OZZY_H)
    assert "output_width" not in fields
    assert "output_height" not in fields


# -- The auto sentinel ------------------------------------------------------


@pytest.mark.parametrize("knob", ["denoise", "sharpen", "fix_compression", "strength"])
def test_negative_one_omits_the_field(node: Any, knob: str) -> None:
    """-1 means 'let Topaz auto-tune', which is omission — not 0, which means 'none'."""
    assert knob not in node._build_fields(OZZY_W, OZZY_H)


def test_explicit_zero_is_sent(node: Any) -> None:
    node.parameter_values.update({"denoise": 0.0, "sharpen": 0.35})
    fields = node._build_fields(OZZY_W, OZZY_H)
    assert fields["denoise"] == "0.0"
    assert fields["sharpen"] == "0.35"


def test_face_enhancement_off_drops_its_sub_fields(node: Any) -> None:
    node.parameter_values["face_enhancement"] = False
    fields = node._build_fields(OZZY_W, OZZY_H)
    assert fields["face_enhancement"] == "false"
    assert "face_enhancement_strength" not in fields
    assert "face_enhancement_creativity" not in fields


def test_out_of_range_creativity_is_clamped(node: Any) -> None:
    node.parameter_values["face_enhancement_creativity"] = 5.0
    assert node._build_fields(OZZY_W, OZZY_H)["face_enhancement_creativity"] == "1.0"


# -- Enum casing ------------------------------------------------------------
# Live 400: 'parameter "subject_detection" must be one of [All Foreground Background]'.
# The published docs show both casings on different pages; the server is the authority.


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ("all", "All"),  # what older saved workflows already hold
        ("All", "All"),
        ("foreground", "Foreground"),
        ("BACKGROUND", "Background"),
        ("", "All"),
        (None, "All"),
        ("nonsense", "All"),
    ],
)
def test_subject_detection_is_normalised_to_title_case(node: Any, saved: Any, expected: str) -> None:
    node.parameter_values["subject_detection"] = saved
    assert node._build_fields(OZZY_W, OZZY_H)["subject_detection"] == expected


def test_subject_detection_choices_are_title_case() -> None:
    assert MODULE.SUBJECT_DETECTION == ["All", "Foreground", "Background"]


@pytest.mark.parametrize(
    ("saved", "expected"),
    [("png", "png"), ("JPEG", "jpeg"), ("tif", "tif"), ("webp", "png")],
)
def test_output_format_is_lowercased_and_validated(node: Any, saved: str, expected: str) -> None:
    node.parameter_values["output_format"] = saved
    assert node._build_fields(OZZY_W, OZZY_H)["output_format"] == expected


def test_webp_is_not_an_accepted_output_format() -> None:
    """Fine as an input, rejected on output."""
    assert "webp" not in MODULE.OUTPUT_FORMATS


def test_model_names_stay_verbatim() -> None:
    assert MODULE.MODELS[0] == "High Fidelity V2"


# -- Limits and the letterbox tripwire --------------------------------------


def test_within_limits_passes(node: Any) -> None:
    node._check_limits(b"x" * 1000, OZZY_W, OZZY_H)


def test_over_limit_input_is_rejected_before_upload(node: Any) -> None:
    node.parameter_values["output_long_edge"] = 40000
    with pytest.raises(ValueError, match="MP"):
        node._check_limits(b"x" * 1000, 30000, 30000)


def test_matching_aspect_does_not_warn(node: Any) -> None:
    assert node._aspect_note(OZZY_W, OZZY_H, 1080, 1920) == ""


def test_changed_aspect_warns_about_letterboxing(node: Any) -> None:
    note = node._aspect_note(OZZY_W, OZZY_H, 1080, 1500)
    assert "WARNING" in note
    assert "letterbox" in note


@pytest.mark.parametrize(
    ("magic", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, ("source.png", "image/png")),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, ("source.jpg", "image/jpeg")),
        (b"RIFF\x00\x00\x00\x00WEBP", ("source.webp", "image/webp")),
    ],
)
def test_mime_sniffing(node: Any, magic: bytes, expected: tuple[str, str]) -> None:
    assert node._source_naming(magic) == expected


# -- Download response ------------------------------------------------------
# The endpoint returns {download_url, head_url, expiry}, not {url}.


class FakeResponse:
    def __init__(self, *, json_body: dict | None = None, content: bytes = b"", ctype: str | None = None) -> None:
        self._json = json_body
        self.content = content
        self.status_code = 200
        self.headers = {"Content-Type": ctype or ("application/json" if json_body is not None else "image/png")}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict | None:
        return self._json

    @property
    def text(self) -> str:
        return json.dumps(self._json) if self._json is not None else ""


REAL_PAYLOAD = {
    "head_url": "https://kosmos-prod.r2.cloudflarestorage.com/output/019fe259.png?X-Amz-Signature=HEADSIG",
    "download_url": "https://kosmos-prod.r2.cloudflarestorage.com/output/019fe259.png?X-Amz-Signature=GETSIG",
    "expiry": 1786000000,
}


def test_download_uses_download_url_not_head_url(node: Any) -> None:
    """head_url is presigned for HEAD; S3 binds a signature to its method, so a GET would 403."""
    fetched: dict[str, str] = {}

    def fake_get(url: str, **_: Any) -> FakeResponse:
        if "/image/v1/download/" in url:
            return FakeResponse(json_body=REAL_PAYLOAD)
        fetched["url"] = url
        return FakeResponse(content=b"\x89PNG\r\n\x1a\nBYTES")

    with patch.object(MODULE.requests, "get", fake_get):
        assert node._download("key", PROCESS_ID) == b"\x89PNG\r\n\x1a\nBYTES"
    assert fetched["url"] == REAL_PAYLOAD["download_url"]


def test_download_tolerates_a_direct_byte_body(node: Any) -> None:
    with patch.object(MODULE.requests, "get", lambda url, **kw: FakeResponse(content=b"RAW")):
        assert node._download("key", PROCESS_ID) == b"RAW"


def test_unknown_download_shape_reports_the_keys_it_saw(node: Any) -> None:
    payload = {"head_url": "x", "expiry": 1}
    with patch.object(MODULE.requests, "get", lambda url, **kw: FakeResponse(json_body=payload)):
        with pytest.raises(RuntimeError, match=r"\['expiry', 'head_url'\]"):
            node._download("key", PROCESS_ID)
