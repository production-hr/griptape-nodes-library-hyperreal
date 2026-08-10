"""The manifest must stay consistent with what is on disk, and every node must construct."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import load_node_module


def test_every_node_file_exists(lib_dir: Path, manifest: dict) -> None:
    for node in manifest["nodes"]:
        assert (lib_dir / node["file_path"]).is_file(), f"{node['class_name']}: missing {node['file_path']}"


def test_every_node_category_is_declared(manifest: dict, categories: set[str]) -> None:
    for node in manifest["nodes"]:
        category = node["metadata"]["category"]
        assert category in categories, f"{node['class_name']}: undeclared category {category!r}"


def test_class_names_and_display_names_are_unique(manifest: dict) -> None:
    class_names = [node["class_name"] for node in manifest["nodes"]]
    display_names = [node["metadata"]["display_name"] for node in manifest["nodes"]]
    assert len(class_names) == len(set(class_names))
    assert len(display_names) == len(set(display_names))


def test_manifest_version_matches_pyproject(lib_dir: Path, manifest: dict) -> None:
    """Three places declare the version; they drifted once and shipped 0.7.0 against a 0.9.1 manifest."""
    pyproject = (lib_dir.parent / "pyproject.toml").read_text(encoding="utf-8")
    declared = next(line for line in pyproject.splitlines() if line.startswith("version = "))
    assert manifest["metadata"]["library_version"] in declared, (
        f"manifest says {manifest['metadata']['library_version']}, pyproject says {declared}"
    )


@pytest.mark.parametrize(
    "class_name",
    [
        "HeyGenAvatarVideo",
        "HeyGenVideoTranslate",
        "TopazVideoUpscale",
        "TopazImageUpscale",
        "WaveSpeedImageEdit",
        "WaveSpeedInfiniteTalk",
        "WaveSpeedInfiniteTalkV2V",
        "DetectHeadRegion",
        "CropToRegion",
        "CompositeRegionBack",
        "ZoomToHead",
        "OverlayZoomedVideo",
        "CompositeOverBackground",
        "ShotSettings",
        "UploadToSpaces",
    ],
)
def test_node_constructs(nodes_by_class: dict[str, dict], class_name: str) -> None:
    entry = nodes_by_class[class_name]
    module = load_node_module(entry["file_path"], f"hr_probe_{class_name}")
    node = getattr(module, class_name)(name=f"probe_{class_name}")
    assert node.parameters, f"{class_name} declared no parameters"


def test_parametrised_list_covers_the_manifest(manifest: dict) -> None:
    """Guard against a new node being added without a construction test."""
    parametrize = next(m for m in test_node_constructs.pytestmark if m.name == "parametrize")
    covered = set(parametrize.args[1])
    declared = {node["class_name"] for node in manifest["nodes"]}
    assert covered == declared, f"untested: {declared - covered}; stale: {covered - declared}"
