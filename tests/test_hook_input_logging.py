"""Privacy-safe diagnostics for hook stdin."""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from biff._hook_entry import _cc_session_start
from biff.hook import _read_hook_input


@pytest.mark.parametrize(
    "payload", [b"private non-json", b'["private value"]', b"null", b"\xff"]
)
def test_malformed_session_start(
    payload: bytes, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed input preserves fail-open output and emits one safe warning."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    with (
        os.fdopen(read_fd) as stdin,
        patch("sys.stdin", stdin),
        patch("biff.hook._open_session") as open_session,
        patch("biff.hook.handle_session_start", return_value="context"),
    ):
        _cc_session_start()
    open_session.assert_called_once_with({})
    assert capsys.readouterr().out == (
        '{"hookSpecificOutput": {"hookEventName": "SessionStart", '
        '"additionalContext": "context"}}\n'
    )
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "hook_input_malformed")
    ]
    assert caplog.records[0].exc_info is None
    assert caplog.records[0].stack_info is None


@pytest.mark.parametrize("payload", [b"", b" \n", b"{}", b'{"key":"value"}'])
def test_empty_and_valid_input_silent(
    payload: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """Empty stdin and JSON objects are not malformed."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    with os.fdopen(read_fd) as stdin, patch("sys.stdin", stdin):
        _read_hook_input()
    assert not caplog.records


def test_input_read_failure(caplog: pytest.LogCaptureFixture) -> None:
    """I/O faults are errors, not malformed input warnings."""
    with patch("sys.stdin.fileno", side_effect=OSError("private exception")):
        assert _read_hook_input() == {}
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.ERROR, "hook_input_read_failed")
    ]
