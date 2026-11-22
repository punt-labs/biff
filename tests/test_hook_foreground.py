"""Foreground hook failures retain one privacy-safe diagnostic."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from biff._hook_dispatch import HookDispatch


def test_foreground_failure(caplog: pytest.LogCaptureFixture) -> None:
    """The original exception propagates with one sanitized ERROR record."""
    error = RuntimeError("secret-payload")
    with (
        patch("biff._hook_foreground._dispatch", side_effect=error),
        pytest.raises(RuntimeError) as raised,
    ):
        HookDispatch.run(detached=False)
    assert raised.value is error
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert record.getMessage().startswith(
        "hook_foreground_failed error=RuntimeError at biff._hook_foreground.invoke:"
    )
    assert record.getMessage().rsplit(":", 1)[1].isdigit()
    assert "secret-payload" not in caplog.text
    assert record.exc_info is None
    assert record.stack_info is None


def test_foreground_success(caplog: pytest.LogCaptureFixture) -> None:
    """Successful foreground dispatch is silent."""
    with patch("biff._hook_foreground._dispatch") as dispatch:
        HookDispatch.run(detached=False)
    dispatch.assert_called_once_with()
    assert not caplog.records


def test_foreground_exit(caplog: pytest.LogCaptureFixture) -> None:
    """An intentional process exit is not an unexpected fault."""
    with (
        patch("biff._hook_foreground._dispatch", side_effect=SystemExit(2)),
        pytest.raises(SystemExit) as raised,
    ):
        HookDispatch.run(detached=False)
    assert raised.value.code == 2
    assert not caplog.records


def test_detached_failure_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    """Detached cleanup keeps its existing records without a foreground duplicate."""
    with (
        caplog.at_level(logging.INFO),
        patch(
            "biff._hook_foreground.ForegroundHook.invoke",
            side_effect=RuntimeError("secret"),
        ),
        patch("biff._hook_foreground.ForegroundHook.run") as foreground,
        pytest.raises(RuntimeError),
    ):
        HookDispatch.run(detached=True)
    foreground.assert_not_called()
    assert [record.levelno for record in caplog.records] == [
        logging.INFO,
        logging.ERROR,
    ]
    assert caplog.records[0].getMessage().startswith("Starting session cleanup: pid=")
    assert caplog.records[1].getMessage().startswith("Session cleanup failed: pid=")
    assert "error=RuntimeError at biff._hook_dispatch._run_detached:" in caplog.text
    assert "secret" not in caplog.text
