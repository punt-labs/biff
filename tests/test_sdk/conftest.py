"""Fixtures for Claude Agent SDK acceptance tests."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

from biff.testing import Transcript

from ._client import SDKClient

pytestmark = pytest.mark.sdk

_TRANSCRIPT_DIR = Path(__file__).parent.parent / "transcripts"


@pytest.fixture(autouse=True)
def _require_api_key() -> None:  # pyright: ignore[reportUnusedFunction]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")


@pytest.fixture
def shared_data_dir(tmp_path: Path) -> Path:
    """Shared data directory for cross-user state."""
    return tmp_path


@pytest.fixture
def transcript(request: pytest.FixtureRequest) -> Generator[Transcript]:
    """Transcript that auto-saves when marked ``@pytest.mark.transcript``."""
    t = Transcript(title="")
    yield t
    node = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]
    marker = node.get_closest_marker("transcript")
    if marker and t.entries:
        _TRANSCRIPT_DIR.mkdir(exist_ok=True)
        slug = node.name.replace("[", "_").replace("]", "")
        path = _TRANSCRIPT_DIR / f"{slug}.txt"
        path.write_text(t.render())


@pytest.fixture
def kai(shared_data_dir: Path, transcript: Transcript) -> SDKClient:
    """SDK client for user kai."""
    return SDKClient(user="kai", data_dir=shared_data_dir, transcript=transcript)


@pytest.fixture
def eric(shared_data_dir: Path, transcript: Transcript) -> SDKClient:
    """SDK client for user eric."""
    return SDKClient(user="eric", data_dir=shared_data_dir, transcript=transcript)
