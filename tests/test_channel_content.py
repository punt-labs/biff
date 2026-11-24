"""Channel event content: fixed templates, no bodies, claimed senders (DES-087)."""

from __future__ import annotations

import pytest

from biff.server.channel_content import (
    MAX_EVENT_LENGTH,
    ChannelEvent,
    EventKind,
    Sender,
)

_OWN = "kai"


class TestSender:
    def test_a_valid_sender_is_claimed_and_unverified(self) -> None:
        sender = Sender.claimed("eric", "tty9", own_user=_OWN)
        assert str(sender) == "eric:tty9 (claimed, unverified)"

    def test_the_sessions_own_identity_claims_to_be_you(self) -> None:
        sender = Sender.claimed("kai", "tty9", own_user=_OWN)
        assert str(sender) == "unverified (claims to be you)"

    def test_a_128_char_user_is_a_teammate(self) -> None:
        sender = Sender.claimed("e" * 128, "tty9", own_user=_OWN)
        assert str(sender) == "a teammate (claimed, unverified)"

    def test_a_40_char_user_is_a_teammate_and_39_is_shown(self) -> None:
        long = Sender.claimed("e" * 40, "t", own_user=_OWN)
        assert str(long).startswith("a teammate")
        assert str(Sender.claimed("e" * 39, "t", own_user=_OWN)).startswith("e" * 39)

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
        sender = Sender.claimed(user, tty, own_user=_OWN)
        assert str(sender) == "a teammate (claimed, unverified)"


class TestTemplates:
    def test_mail_names_a_count_and_no_sender(self) -> None:
        event = ChannelEvent.mail(3)
        assert event.content == "Call read_messages: 3 unread biff messages."
        assert event.meta == {"kind": "mail", "count": "3"}

    def test_one_message_is_singular(self) -> None:
        assert (
            ChannelEvent.mail(1).content == "Call read_messages: 1 unread biff message."
        )

    def test_wall_names_a_claimed_sender(self) -> None:
        sender = Sender.claimed("eric", "tty9", own_user=_OWN)
        event = ChannelEvent.wall(sender)
        assert event.content == (
            "Call wall to read: new biff wall from eric:tty9 (claimed, unverified)."
        )
        assert event.meta == {"kind": "wall"}

    def test_a_talk_invite_tells_the_model_to_ask_the_user(self) -> None:
        sender = Sender.claimed("eric", "tty9", own_user=_OWN)
        event = ChannelEvent.talk_invite(sender)
        assert event.content.startswith("Ask the user")
        assert "accept" not in event.content.lower()
        assert event.meta == {"kind": "talk_invite"}

    def test_a_talk_message_names_no_sender(self) -> None:
        event = ChannelEvent.talk_message()
        assert event.content == "Call talk_read: new message from your talk partner."
        assert event.meta == {"kind": "talk_message"}

    @pytest.mark.parametrize(
        "event",
        [
            ChannelEvent.mail(10**9),
            ChannelEvent.wall(Sender.claimed("e" * 39, "t" * 128, own_user=_OWN)),
            ChannelEvent.talk_invite(
                Sender.claimed("e" * 39, "t" * 128, own_user=_OWN)
            ),
        ],
    )
    def test_no_event_exceeds_the_length_bound(self, event: ChannelEvent) -> None:
        assert len(event.content) <= MAX_EVENT_LENGTH

    def test_meta_uses_a_closed_key_set(self) -> None:
        kinds = {kind.value for kind in EventKind}
        assert kinds == {"mail", "wall", "talk_invite", "talk_message"}


class TestFitsOneLine:
    """The client shows one line: a typical event fits 80 columns."""

    @pytest.mark.parametrize(
        "event",
        [
            ChannelEvent.mail(120),
            ChannelEvent.wall(Sender.claimed("eric", "tty9", own_user=_OWN)),
            ChannelEvent.talk_invite(Sender.claimed("eric", "tty9", own_user=_OWN)),
            ChannelEvent.talk_message(),
        ],
    )
    def test_a_typical_event_is_at_most_80_columns(self, event: ChannelEvent) -> None:
        assert len(event.content) <= 80

    def test_the_action_comes_first(self) -> None:
        sender = Sender.claimed("eric", "tty9", own_user=_OWN)
        starts = [
            ChannelEvent.mail(2).content,
            ChannelEvent.wall(sender).content,
            ChannelEvent.talk_invite(sender).content,
            ChannelEvent.talk_message().content,
        ]
        assert [c.split(":")[0] for c in starts] == [
            "Call read_messages",
            "Call wall to read",
            "Ask the user",
            "Call talk_read",
        ]
