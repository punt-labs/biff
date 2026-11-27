"""The terminal rendering of talk: ANSI lines, banners, the timestamp toggle.

The protocol state machine is covered by ``tests/test_talk_state.py`` and the
daemon methods by ``tests/test_daemon/test_talk.py``.  These tests cover how a
``talk_read`` reading is drawn: colour, wrapping, neutralised escapes, and the
once-only announcement of an invite.
"""

from __future__ import annotations

import re
from typing import Literal

from biff.cli_talk_view import TalkView
from biff.repl_display import ReplDisplay
from biff.talk_reading import TalkReading
from biff.talk_types import TalkNotification
from biff.talk_wire import WireInvite, WireMessage

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    """Drop SGR colour codes so plain layout can be asserted."""
    return _ANSI.sub("", s)


def _message(
    kind: Literal["message", "end"],
    nfrom: str = "eric",
    nfrom_tty: str = "tty2",
    body: str = "",
) -> WireMessage:
    """A message as biffd sends it: the label is neutralised by the daemon."""
    notif = TalkNotification(
        ntype=kind, nfrom=nfrom, nfrom_tty=nfrom_tty, nfrom_key="", nto="", nbody=body
    )
    return WireMessage(kind=kind, label=notif.sender_label, body=body)


def _invite(user: str = "eric", tty: str = "tty2") -> WireInvite:
    return WireInvite.model_validate(
        {
            "from": user,
            "tty": tty,
            "label": f"{user}:{tty}",
            "accept": f"talk {user}:{tty}",
        }
    )


def _reading(
    messages: tuple[WireMessage, ...] = (), invites: tuple[WireInvite, ...] = ()
) -> TalkReading:
    return TalkReading(
        lines=(),
        messages=messages,
        invites=invites,
        ended=False,
        accept_published=True,
        opening="",
        opening_unsent=False,
        dropped=0,
        phase="idle",
        peer="",
    )


def _view(*, stamped: bool = False) -> TalkView:
    display = ReplDisplay()
    display.set_timestamps(on=stamped)
    return TalkView(display)


class TestConversation:
    def test_empty(self) -> None:
        assert _view().conversation(()) == []

    def test_message_conversation_style(self) -> None:
        lines = _view().conversation((_message("message", body="hello there"),))
        assert len(lines) == 1
        assert "▶  eric:tty2  hello there" in lines[0]
        assert "\033[36m" in lines[0]  # cyan
        assert "📞" not in lines[0]

    def test_message_without_tty(self) -> None:
        lines = _view().conversation((_message("message", nfrom_tty="", body="hi"),))
        assert "▶  eric  hi" in lines[0]

    def test_control_only_body_renders_fallback(self) -> None:
        # A body empty only after neutralisation still arrived: the reader sees
        # an explanatory fallback line, not silence.
        lines = _view().conversation((_message("message", body="\x00\x1b\x07"),))
        assert len(lines) == 1
        assert "no printable text" in lines[0]

    def test_long_message_wraps_with_aligned_continuation(self) -> None:
        body = ("word " * 30).strip()  # far wider than the 80-column table
        lines = _view().conversation((_message("message", body=body),))
        assert len(lines) > 1
        assert "▶  eric:tty2  " in lines[0]
        assert "▶" not in _strip_ansi(lines[1])
        assert _strip_ansi(lines[1]).startswith("   ")

    def test_empty_body_message_not_formatted(self) -> None:
        assert _view().conversation((_message("message", body=""),)) == []

    def test_end_renders_hangup(self) -> None:
        lines = _view().conversation((_message("end"),))
        assert len(lines) == 1
        assert "ended the conversation" in lines[0]
        assert "eric:tty2" in lines[0]
        assert "\033[2m" in lines[0]  # dim

    def test_end_without_tty(self) -> None:
        lines = _view().conversation((_message("end", nfrom_tty=""),))
        assert "eric has ended" in lines[0]

    def test_mixed_message_and_end(self) -> None:
        lines = _view().conversation((_message("message", body="bye"), _message("end")))
        assert len(lines) == 2
        assert "bye" in lines[0]
        assert "ended the conversation" in lines[1]

    def test_no_timestamp_when_display_off(self) -> None:
        lines = _view().conversation((_message("message", body="hi"),))
        assert re.search(r"\[\d{2}:\d{2}\]", lines[0]) is None

    def test_timestamp_prefix_when_display_on(self) -> None:
        lines = _view(stamped=True).conversation((_message("message", body="hello"),))
        assert re.search(r"▶  \[\d{2}:\d{2}\] eric:tty2  hello", lines[0]) is not None

    def test_escape_injection_in_body_neutralized(self) -> None:
        body = "clear\x1b[2Jme\x1b]0;pwned\x07"
        lines = _view().conversation((_message("message", body=body),))
        assert "\x1b[2J" not in lines[0]
        assert "\x1b]0;" not in lines[0]
        assert "\x07" not in lines[0]
        assert "clear[2Jme]0;pwned" in lines[0]

    def test_escape_injection_in_sender_neutralized(self) -> None:
        lines = _view().conversation(
            (_message("message", nfrom="e\x1b[2Jvil", body="hi"),)
        )
        assert "\x1b[2J" not in lines[0]
        assert "e[2Jvil:tty2  hi" in lines[0]


class TestBanners:
    def test_empty(self) -> None:
        assert _view().banners(_reading()) == []

    def test_invite_renders_talk_idiom(self) -> None:
        lines = _view().banners(_reading(invites=(_invite(),)))
        assert len(lines) == 1
        assert "▶  eric:tty2  wants to talk — talk eric:tty2 to accept" in lines[0]
        assert "📞" not in lines[0]
        assert "\033[1;33m" in lines[0]  # yellow

    def test_an_invite_is_announced_once_while_it_stays_pending(self) -> None:
        view = _view()
        reading = _reading(invites=(_invite(),))
        assert len(view.banners(reading)) == 1
        assert view.banners(reading) == []

    def test_an_invite_answered_and_sent_again_is_announced_again(self) -> None:
        view = _view()
        view.banners(_reading(invites=(_invite(),)))
        view.banners(_reading())  # answered or withdrawn: no longer pending
        assert len(view.banners(_reading(invites=(_invite(),)))) == 1

    def test_message_shows_sender_prefix(self) -> None:
        lines = _view().banners(_reading((_message("message", body="hi there"),)))
        assert len(lines) == 1
        assert "eric:tty2" in lines[0]
        assert "hi there" in lines[0]
        assert "\033[1;33m" in lines[0]  # yellow, not the conversation's cyan

    def test_end_renders_nothing_at_an_idle_prompt(self) -> None:
        assert _view().banners(_reading((_message("end"),))) == []

    def test_control_only_body_renders_fallback(self) -> None:
        lines = _view().banners(_reading((_message("message", body="\x00\x1b\x07"),)))
        assert len(lines) == 1
        assert "no printable text" in lines[0]

    def test_banner_stamped_when_display_on(self) -> None:
        lines = _view(stamped=True).banners(
            _reading((_message("message", body="hi there"),))
        )
        pattern = r"▶  \[\d{2}:\d{2}\] eric:tty2  hi there"
        assert re.search(pattern, lines[0]) is not None

    def test_banner_escape_injection_neutralized(self) -> None:
        lines = _view().banners(_reading((_message("message", body="hi\x1b[2Jthere"),)))
        assert "\x1b[2J" not in lines[0]
        assert "hi[2Jthere" in lines[0]

    def test_an_invite_accept_command_cannot_inject_escapes(self) -> None:
        hostile = WireInvite.model_validate(
            {
                "from": "eric",
                "tty": "tty2",
                "label": "eric:tty2",
                "accept": "talk eric:tty2\x1b[2J",
            }
        )
        lines = _view().banners(_reading(invites=(hostile,)))
        assert "\x1b[2J" not in lines[0]


class TestInvites:
    def test_only_invites_become_banners_while_waiting_for_an_accept(self) -> None:
        reading = _reading((_message("message", body="opening line"),), (_invite(),))
        lines = _view().invites(reading)
        assert len(lines) == 1
        assert "opening line" not in lines[0]


class TestDropped:
    def test_lost_lines_are_announced(self) -> None:
        reading = _reading().model_copy(update={"dropped": 3})
        lines = _view().banners(reading)
        assert len(lines) == 1
        assert "3 talk lines were lost" in lines[0]

    def test_nothing_is_said_when_nothing_was_lost(self) -> None:
        assert _view().dropped(_reading()) == []
