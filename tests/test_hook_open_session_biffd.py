"""With ``mcp.transport: daemon`` SessionStart asks biffd, not a relay."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from biff.client.environment import UnsupportedOverrideError
from biff.plugins.claude_code.daemon_session import DaemonSessionOpener
from biff.plugins.claude_code.hook import _open_session

if TYPE_CHECKING:
    import pytest


class _Recording:
    def __init__(self) -> None:
        self.asked: list[dict[str, object]] = []

    async def open(self, data: dict[str, object]) -> str:
        self.asked.append(data)
        return "k"


class _Down:
    async def open(self, data: dict[str, object]) -> str:
        msg = "biffd is down"
        raise ConnectionError(msg)


async def _in_process(data: dict[str, object], **_kw: object) -> str | None:
    msg = f"the in-process path must not run for {data}"
    raise AssertionError(msg)


def test_the_daemon_opener_runs_and_the_in_process_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
    opener = _Recording()
    monkeypatch.setattr(DaemonSessionOpener, "for_host", lambda: opener)

    with patch("biff.session_open.open_session_from_payload", _in_process):
        _open_session({"session_id": "sid-1"})

    assert opener.asked == [{"session_id": "sid-1"}]


def test_a_daemon_failure_is_logged_and_does_not_break_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
    monkeypatch.setattr(DaemonSessionOpener, "for_host", lambda: _Down())

    _open_session({"session_id": "sid-1"})  # must not raise

    assert "biffd is down" in caplog.text


class _Refused:
    async def open(self, data: dict[str, object]) -> str:
        raise UnsupportedOverrideError(
            "BIFF_RELAY_URL cannot be sent to biffd yet and would be ignored; "
            "unset it, or run biff without the daemon with "
            "BIFF_MCP_TRANSPORT=inprocess (or mcp.transport: inprocess in "
            ".punt-labs/biff/config.yaml)"
        )


def test_a_session_that_could_not_be_opened_says_why_and_how_to_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
    monkeypatch.setattr(DaemonSessionOpener, "for_host", lambda: _Refused())

    note = _open_session({"session_id": "sid-1"})

    assert "BIFF_RELAY_URL" in note
    assert "biff doctor" in note


def test_an_unreachable_biffd_is_a_sentence_for_the_model_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
    monkeypatch.setattr(DaemonSessionOpener, "for_host", lambda: _Down())

    note = _open_session({"session_id": "sid-1"})

    assert "biffd is down" in note
    assert "biff install" in note


def test_a_session_that_opened_has_nothing_to_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
    monkeypatch.setattr(DaemonSessionOpener, "for_host", lambda: _Recording())

    assert _open_session({"session_id": "sid-1"}) == ""


def test_the_start_context_carries_the_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from biff.plugins.claude_code import hook

    monkeypatch.setattr(hook, "_read_hook_input", lambda: {"session_id": "sid-1"})

    def _failed(_data: dict[str, object]) -> str:
        return "SESSION NOT OPENED"

    monkeypatch.setattr(hook, "_open_session", _failed)

    hook._run_session_start(resume=False)  # pyright: ignore[reportPrivateUsage]

    assert "SESSION NOT OPENED" in capsys.readouterr().out
