"""Shared fixtures.

Node files are self-contained by library convention and are never imported as a
package, so tests load each one from its path the same way the engine does.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "hyperreal"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def load_node_module(relative_path: str, name: str | None = None) -> ModuleType:
    """Import a node file by path, as the engine's library loader does."""
    path = LIB / relative_path
    module_name = name or f"hr_test_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        msg = f"Could not load {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def lib_dir() -> Path:
    return LIB


@pytest.fixture(scope="session")
def manifest() -> dict:
    return json.loads((LIB / "griptape_nodes_library.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def categories(manifest: dict) -> set[str]:
    return {key for entry in manifest["categories"] for key in entry}


@pytest.fixture(scope="session")
def nodes_by_class(manifest: dict) -> dict[str, dict]:
    return {node["class_name"]: node for node in manifest["nodes"]}


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    """Path to the ffmpeg binary, skipping the test if it cannot be fetched."""
    try:
        import static_ffmpeg.run

        return static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()[0]
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"ffmpeg unavailable: {exc}")
        raise
