"""Tests for the REPL talk presentation layer (biff.__main__ formatters).

The talk *protocol* state machine lives in ``biff.talk_state`` and is
covered by ``tests/test_talk_state.py``.  These tests cover the CLI's
*rendering* of drained notifications — the ANSI banners, the timestamp
toggle, and terminal-escape neutralisation — which is the REPL front-end's
responsibility (talk.tex Drain* display side).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from biff.__main__ import (
    _format_idle_banners,
    _format_talk_lines,
    _print_talk_banner,
)
from biff.repl_display import ReplDisplay
from biff.talk_types import TalkNotification

if TYPE_CHECKING:
    import pytest

OTHER_KEY = "eric:def67890"


def _notif(
    ntype: str,
    nfrom: str = "eric",
    nfrom_tty: str = "tty2",
    body: str = "",
    from_key: str = OTHER_KEY,
) -> TalkNotification:
    return TalkNotification(
        ntype=ntype,
        nfrom=nfrom,
        nfrom_tty=nfrom_tty,
        nfrom_key=from_key,
        nto="",
        nbody=body,
    )


# ---------------------------------------------------------------------------
# _format_talk_lines — connected-mode rendering
# ---------------------------------------------------------------------------


class TestFormatTalkLines:
    def test_empty(self) -> None:
        assert _format_talk_lines([]) == []

    def test_message_conversation_style(self) -> None:
        lines = _format_talk_lines([_notif("message", body="hello there")])
        assert len(lines) == 1
        assert "eric:tty2" in lines[0]
        assert "hello there" in lines[0]
        assert "\033[36m" in lines[0]  # cyan
        assert "📞" not in lines[0]

    def test_message_without_tty(self) -> None:
        lines = _format_talk_lines([_notif("message", nfrom_tty="", body="hi")])
        assert "eric ▶ hi" in lines[0]

    def test_empty_body_message_not_formatted(self) -> None:
        assert _format_talk_lines([_notif("message", body="")]) == []

    def test_end_renders_hangup(self) -> None:
        lines = _format_talk_lines([_notif("end")])
        assert len(lines) == 1
        assert "ended the conversation" in lines[0]
        assert "eric:tty2" in lines[0]

    def test_end_without_tty(self) -> None:
        lines = _format_talk_lines([_notif("end", nfrom_tty="")])
        assert "eric has ended" in lines[0]

    def test_multiple_messages(self) -> None:
        lines = _format_talk_lines(
            [_notif("message", body="first"), _notif("message", body="second")]
        )
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]

    def test_mixed_message_and_end(self) -> None:
        lines = _format_talk_lines([_notif("message", body="bye"), _notif("end")])
        assert len(lines) == 2
        assert "bye" in lines[0]
        assert "ended the conversation" in lines[1]

    def test_no_timestamp_without_display(self) -> None:
        lines = _format_talk_lines([_notif("message", body="hi")])
        assert re.search(r"\[\d{2}:\d{2}\]", lines[0]) is None

    def test_no_timestamp_when_display_off(self) -> None:
        lines = _format_talk_lines([_notif("message", body="hi")], ReplDisplay())
        assert re.search(r"\[\d{2}:\d{2}\]", lines[0]) is None

    def test_timestamp_prefix_when_display_on(self) -> None:
        display = ReplDisplay()
        display.set_timestamps(on=True)
        lines = _format_talk_lines([_notif("message", body="hello")], display)
        assert re.search(r"\[\d{2}:\d{2}\] eric:tty2 ▶ hello", lines[0]) is not None

    def test_escape_injection_in_body_neutralized(self) -> None:
        lines = _format_talk_lines(
            [_notif("message", body="clear\x1b[2Jme\x1b]0;pwned\x07")]
        )
        assert "\x1b[2J" not in lines[0]
        assert "\x1b]0;" not in lines[0]
        assert "\x07" not in lines[0]
        assert "clear[2Jme]0;pwned" in lines[0]

    def test_escape_injection_in_sender_neutralized(self) -> None:
        lines = _format_talk_lines([_notif("message", nfrom="e\x1b[2Jvil", body="hi")])
        assert "\x1b[2J" not in lines[0]
        assert "e[2Jvil:tty2 ▶ hi" in lines[0]


# ---------------------------------------------------------------------------
# _format_idle_banners — idle-mode rendering
# ---------------------------------------------------------------------------


class TestFormatIdleBanners:
    def test_empty(self) -> None:
        assert _format_idle_banners([]) == []

    def test_invite_renders_phone_banner(self) -> None:
        lines = _format_idle_banners([_notif("invite", body="wants to talk")])
        assert len(lines) == 1
        assert "📞" in lines[0]
        assert "wants to talk" in lines[0]

    def test_accept_is_silent(self) -> None:
        assert _format_idle_banners([_notif("accept")]) == []

    def test_message_shows_sender_prefix(self) -> None:
        lines = _format_idle_banners([_notif("message", body="hi there")])
        assert len(lines) == 1
        assert "eric:tty2" in lines[0]
        assert "hi there" in lines[0]

    def test_end_without_body_renders_nothing(self) -> None:
        assert _format_idle_banners([_notif("end")]) == []

    def test_banner_stamped_when_display_on(self) -> None:
        display = ReplDisplay()
        display.set_timestamps(on=True)
        lines = _format_idle_banners([_notif("message", body="hi there")], display)
        assert re.search(r"\[\d{2}:\d{2}\] eric:tty2 ▶ hi there", lines[0]) is not None

    def test_banner_escape_injection_neutralized(self) -> None:
        lines = _format_idle_banners([_notif("message", body="hi\x1b[2Jthere")])
        assert "\x1b[2J" not in lines[0]
        assert "hi[2Jthere" in lines[0]


# ---------------------------------------------------------------------------
# _print_talk_banner — third-party banner during the accept wait
# ---------------------------------------------------------------------------


class TestPrintTalkBanner:
    def test_prints_banner_with_body(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_talk_banner(_notif("invite", nfrom="priya", body="wants to talk"))
        out = capsys.readouterr().out
        assert "priya" in out
        assert "wants to talk" in out
        assert "📞" in out

    def test_no_body_prints_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_talk_banner(_notif("invite", body=""))
        assert capsys.readouterr().out == ""
