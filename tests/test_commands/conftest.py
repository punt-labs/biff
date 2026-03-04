"""Shared fixtures for command tests.

Every command test gets a ``CliContext`` backed by a ``LocalRelay``
pointing at ``tmp_path``. No NATS, no network, no mocking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.cli_session import CliContext
from biff.models import BiffConfig
from biff.relay import LocalRelay


@pytest.fixture()
def relay(tmp_path: Path) -> LocalRelay:
    """Fresh LocalRelay backed by tmp_path."""
    return LocalRelay(tmp_path)


@pytest.fixture()
def ctx(relay: LocalRelay) -> CliContext:
    """CliContext for user 'kai' with deterministic session key."""
    return CliContext(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key="kai:abc12345",
        user="kai",
        tty="abc12345",
    )


@pytest.fixture()
def ctx_eric(relay: LocalRelay) -> CliContext:
    """CliContext for user 'eric' sharing the same relay."""
    return CliContext(
        relay=relay,
        config=BiffConfig(user="eric", repo_name="test"),
        session_key="eric:def67890",
        user="eric",
        tty="def67890",
    )
