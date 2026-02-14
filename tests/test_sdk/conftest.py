"""Fixtures for Claude Agent SDK acceptance tests.

Each test spawns a real Claude Code session with biff configured as an MCP
server.  The SDK manages the biff subprocess lifecycle via stdio transport.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.sdk


@pytest.fixture
def shared_data_dir(tmp_path: Path) -> Path:
    """Shared data directory for cross-user state."""
    return tmp_path
