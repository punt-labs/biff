"""Channel event content: fixed templates, no bodies, plain senders (DES-087)."""

from __future__ import annotations

import pytest

from biff.server.channel_content import (
    MAX_EVENT_LENGTH,
    ChannelEvent,
    EventKind,
    Sender,
)
from tests._trust_phrases import assert_no_trust_phrase


class TestSender:
    def test_a_valid_sender_is_shown_as_its_address(self) -> None:
        sender = Sender.claimed("eric", "tty9")
        assert str(sender) == "eric:tty9"

    def test_the_sessions_own_user_is_shown_as_its_address(self) -> None:
        sender = Sender.claimed("kai", "tty9")
        assert str(sender) == "kai:tty9"

    def test_a_128_char_user_is_a_teammate(self) -> None:
        sender = Sender.claimed("e" * 128, "tty9")
        assert str(sender) == "a teammate"

    def test_a_40_char_user_is_a_teammate_and_39_is_shown(self) -> None:
        long = Sender.claimed("e" * 40, "t")
        assert str(long).startswith("a teammate")
        assert str(Sender.claimed("e" * 39, "t")).startswith("e" * 39)

    @pytest.mark.parametrize(
        ("user", "tty"),
        [
            ("er ic", "tty9"),  # a space
            ("eric", "tty 9"),
            ("eric\n", "tty9"),  # fullmatch, not match: no trailing newline
            ("eric", ""),  # a part that is empty is not an identifier
            ("eric", "t" * 129),
            ("ér", "tty9"),
        ],
    )
    def test_a_part_outside_the_identifier_set_is_a_teammate(
        self, user: str, tty: str
    ) -> None:
        sender = Sender.claimed(user, tty)
        assert str(sender) == "a teammate"


class TestTemplates:
    def test_mail_names_a_count_and_no_sender(self) -> None:
        event = ChannelEvent.mail(3)
        assert event.content == "Call read_messages: 3 unread biff messages."
        assert event.meta == {"kind": "mail", "count": "3"}

    def test_one_message_is_singular(self) -> None:
        assert (
            ChannelEvent.mail(1).content == "Call read_messages: 1 unread biff message."
        )

    def test_wall_names_the_sender(self) -> None:
        sender = Sender.claimed("eric", "tty9")
        event = ChannelEvent.wall(sender)
        assert event.content == "Call wall: new wall from eric:tty9."
        assert event.meta == {"kind": "wall"}

    def test_a_talk_invite_names_the_sender_and_talk_read(self) -> None:
        sender = Sender.claimed("eric", "tty9")
        event = ChannelEvent.talk_invite(sender)
        assert event.content == "Call talk_read: talk invite from eric:tty9."
        assert event.meta == {"kind": "talk_invite"}

    def test_a_talk_message_names_no_sender(self) -> None:
        event = ChannelEvent.talk_message()
        assert event.content == "Call talk_read: new message from your talk partner."
        assert event.meta == {"kind": "talk_message"}

    @pytest.mark.parametrize(
        "event",
        [
            ChannelEvent.mail(10**9),
            ChannelEvent.wall(Sender.claimed("e" * 39, "t" * 128)),
            ChannelEvent.talk_invite(Sender.claimed("e" * 39, "t" * 128)),
        ],
    )
    def test_no_event_exceeds_the_length_bound(self, event: ChannelEvent) -> None:
        assert len(event.content) <= MAX_EVENT_LENGTH

    def test_meta_uses_a_closed_key_set(self) -> None:
        kinds = {kind.value for kind in EventKind}
        assert kinds == {"mail", "wall", "talk_invite", "talk_message"}


class TestFitsTheClientsLine:
    """The client shows about 70 characters of one line, prefix included."""

    @pytest.mark.parametrize(
        "event",
        [
            ChannelEvent.mail(120),
            ChannelEvent.wall(Sender.claimed("eric", "tty9")),
            ChannelEvent.wall(Sender.claimed("kai", "tty9")),
            ChannelEvent.talk_invite(Sender.claimed("eric", "tty9")),
            ChannelEvent.talk_message(),
        ],
    )
    def test_a_typical_event_is_at_most_60_characters(
        self, event: ChannelEvent
    ) -> None:
        assert len(event.content) <= 60

    def test_the_action_comes_before_the_sender_and_no_trust_label(self) -> None:
        sender = Sender.claimed("eric", "tty9")
        for event in (ChannelEvent.wall(sender), ChannelEvent.talk_invite(sender)):
            text = event.content
            assert text.index("Call ") < text.index(str(sender))
            assert_no_trust_phrase(text)
        assert ChannelEvent.wall(sender).content.startswith("Call wall:")
        assert ChannelEvent.talk_invite(sender).content.startswith("Call talk_read:")
        assert ChannelEvent.mail(2).content.startswith("Call read_messages:")
        assert ChannelEvent.talk_message().content.startswith("Call talk_read:")
