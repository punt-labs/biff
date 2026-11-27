"""A context that changes keeps the talk state a conversation lives in."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.cli_context import CliContext
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.talk_types import TalkPhase

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def ctx(tmp_path: Path) -> CliContext:
    context = CliContext(
        relay=LocalRelay(tmp_path),
        config=BiffConfig(user="kai", repo_name="test"),
        session_key="kai:abc12345",
        user="kai",
        tty="abc12345",
        tty_name="tty1",
    )
    context.talk.begin_invite(
        partner="eric", partner_tty="tty2", partner_key="eric:def67890"
    )
    return context


def test_new_org_repos_keep_the_conversation(ctx: CliContext) -> None:
    changed = ctx.with_org_repos(frozenset({"acme-api"}))
    assert (changed.org_repos, changed.talk.phase) == (
        frozenset({"acme-api"}),
        TalkPhase.INVITING,
    )


def test_a_new_tty_name_keeps_the_conversation_and_stamps_later_frames(
    ctx: CliContext,
) -> None:
    changed = ctx.with_tty_name("tty9")
    assert (changed.tty_name, changed.talk.phase, changed.talk.my_tty_name) == (
        "tty9",
        TalkPhase.INVITING,
        "tty9",
    )


def test_the_changed_context_shares_the_talk_state(ctx: CliContext) -> None:
    assert ctx.with_org_repos(frozenset()).talk is ctx.talk
