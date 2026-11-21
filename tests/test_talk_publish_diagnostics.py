"""Talk failure diagnostics identify the failed step without exposing content."""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from punt_observe.logging import Role

from biff.commands._talk_relay import publish_failure
from biff.commands.talk import (
    TalkContext,
    end_or_cancel,
    publish_auto_accept,
    send_line,
)
from biff.logging_config import configure_logging
from biff.nats_relay import NatsRelay
from biff.relay import Relay
from biff.talk_state import TalkState
from biff.talk_types import TalkPhase


@pytest.mark.parametrize("operation", ["hangup", "withdraw", "auto-accept", "send"])
@pytest.mark.parametrize("step", ["connection", "publish"])
@pytest.mark.parametrize("message", ["", "private-login private-payload private-error"])
async def test_failure_context_is_safe(
    operation: str, step: str, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError(message)

    failure = AsyncMock(side_effect=fail)
    nc = MagicMock()
    nc.publish = failure if step == "publish" else AsyncMock()
    relay = MagicMock(spec=NatsRelay)
    relay.get_nc = failure if step == "connection" else AsyncMock(return_value=nc)
    relay.talk_notify_subject.return_value = "private-subject.private-login"
    state = TalkState(
        relay=cast("Relay", relay),
        user="private-login",
        tty="private-tty",
        session_key="private-login:private-tty",
    )
    if operation == "withdraw":
        state.begin_invite(
            partner="private-peer", partner_tty="peer-tty", partner_key="private-key"
        )
    else:
        state.begin_connected(
            partner="private-peer", partner_tty="peer-tty", partner_key="private-key"
        )
    ctx = MagicMock(spec=TalkContext)
    ctx.talk = state

    with caplog.at_level(logging.INFO, logger="biff.commands.talk"):
        if operation == "auto-accept":
            assert await publish_auto_accept(ctx, to_key="private-key") is False
            assert state.phase is TalkPhase.CONNECTED
        elif operation == "send":
            result = await send_line(
                ctx,
                to_key="private-key",
                display="private-peer",
                message="private-payload",
            )
            assert result.error is True
            assert result.text == (
                "Could not reach private-peer — message not sent; try again."
            )
            assert state.phase is TalkPhase.CONNECTED
        else:
            result = await end_or_cancel(ctx)
            assert result.error is True
            assert state.phase is TalkPhase.IDLE

    records = [r for r in caplog.records if r.name == "biff.commands.talk"]
    expected_attempts = 2 if operation == "auto-accept" else 1
    assert len(records) == failure.await_count == expected_attempts
    source = f"{__name__}/fail:{fail.__code__.co_firstlineno + 1}"
    for attempt, record in enumerate(records, 1):
        diagnostic = record.getMessage()
        assert f"talk {operation} failed" in diagnostic
        assert f"step={step}" in diagnostic
        assert "error=TimeoutError" in diagnostic
        assert f"source={source}" in diagnostic
        assert record.levelno == logging.INFO
        assert record.exc_info is None
        if operation == "auto-accept":
            assert f"attempt={attempt}/2" in diagnostic
    assert "private" not in caplog.text
    assert "Traceback" not in caplog.text


def test_failure_without_traceback_has_safe_unknown_source() -> None:
    assert publish_failure(OSError("private-login private-payload")) == (
        "step=unknown error=OSError source=unknown"
    )


async def test_send_failure_reaches_file_without_interrupting_repl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    state = MagicMock(spec=TalkState)
    state.phase = TalkPhase.CONNECTED
    state.partner_key = "private-key"
    state.send_message = AsyncMock(side_effect=TimeoutError("private-error"))
    ctx = MagicMock(spec=TalkContext)
    ctx.talk = state
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging(Role.CLI, stderr_level="WARNING")
        result = await send_line(
            ctx, to_key="private-key", display="private-peer", message="private-payload"
        )
        assert result.error is True
        for handler in root.handlers:
            handler.flush()
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    log_file = tmp_path / ".punt-labs" / "biff" / "logs" / "biff.log"
    diagnostic = log_file.read_text(encoding="utf-8")
    assert "[INFO] biff.commands.talk: talk send failed:" in diagnostic
    assert "error=TimeoutError" in diagnostic
    assert "private" not in diagnostic
    assert "Traceback" not in diagnostic
    assert stderr.getvalue() == ""
