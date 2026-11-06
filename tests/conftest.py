"""Shared fixtures: the demo mold as the app loads it, and its cavity for intrusion checks."""
import io
from pathlib import Path

import pytest
import trimesh

from flangecutter.geometry import mesh_io

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "demo_mold.stl"


def pytest_configure(config):
    config.addinivalue_line("markers", "ui: browser test with Playwright (needs `playwright install chromium`)")
    config.addinivalue_line("markers", "slow: takes more than a few seconds")


@pytest.fixture(scope="session")
def sample_bytes() -> bytes:
    return SAMPLE.read_bytes()


@pytest.fixture()
def model(sample_bytes):
    """A fresh model per test, so section caches never leak between tests."""
    return mesh_io.load_stl(sample_bytes, SAMPLE.name)


@pytest.fixture(scope="session")
def cavity():
    from demo_mold import demo_mold
    return demo_mold()[1]


def reload_stl(data: bytes) -> trimesh.Trimesh:
    """What a slicer sees: the STL read back from disk."""
    return trimesh.load_mesh(io.BytesIO(data), file_type="stl")
