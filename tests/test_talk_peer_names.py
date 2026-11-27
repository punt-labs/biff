"""A peer's name comes off the network: it reaches no terminal and no plan raw."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.cli_talk import TalkConversation
from biff.cli_talk_outcome import TalkOutcome
from biff.talk_types import PendingInvite, TalkNotification

if TYPE_CHECKING:
    from biff.cli_link import DaemonLink

HOSTILE_TTY = "tty1\x1b[2J\x1b]52;c;cHduZWQ=\x07\nPWN"


def _invite(tty: str, user: str = "eric") -> TalkNotification:
    return TalkNotification.from_payload(
        {
            "type": "invite",
            "from": user,
            "from_tty": tty,
            "from_key": f"{user}:abc123",
            "to_key": "kai:def456",
            "body": "",
        }
    )


def test_an_invite_keeps_only_printable_characters_of_the_senders_tty() -> None:
    invite = PendingInvite.from_notification(_invite(HOSTILE_TTY))
    assert invite.tty == "tty1[2J]52;c;cHduZWQ=PWN"


def test_an_invite_whose_user_is_not_printable_is_refused() -> None:
    with pytest.raises(ValueError, match="does not match"):
        PendingInvite.from_notification(_invite("tty1", user="er\x1b[2Jic"))


async def test_a_hostile_peer_name_reaches_neither_the_terminal_nor_the_plan() -> None:
    hostile = f"eric:{HOSTILE_TTY}"
    link = MagicMock()
    link.pump.lost = ""
    link.talk = AsyncMock(
        return_value=TalkOutcome(
            text=f"Invite sent to {hostile}.",
            error=False,
            phase="connected",
            peer=hostile,
        )
    )
    link.set_plan = AsyncMock()
    link.talk_end = AsyncMock()
    said: list[str] = []
    console = MagicMock()
    console.prompt = "kai> "
    console.say = lambda text="", *, end="\n": said.append(text + end)
    console.next = AsyncMock(return_value=None)

    conversation = TalkConversation(
        link=cast("DaemonLink", link),
        console=console,
        view=MagicMock(),
        prompt="kai> ",
    )
    await conversation.run(hostile, "")

    planned = [call.args[0] for call in link.set_plan.await_args_list]
    # The only escape biff itself writes is the clear-line before a message.
    for shown in (*said, *planned):
        text = shown.replace("\r\x1b[K", "")
        assert "\x1b" not in text
        assert "\x07" not in text
        assert "\nPWN" not in text
