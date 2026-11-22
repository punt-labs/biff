"""Tests for picking the repo mailbox a user drains."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff.models import UserSession
from biff.server.tools._presence import Presence

_NOW = datetime.now(UTC)
_STALE = _NOW - timedelta(hours=3)


def _session(repo: str, last_active: datetime, user: str = "eric") -> UserSession:
    return UserSession(
        user=user,
        tty=repo[-1],
        repo=repo,
        last_active=last_active,
        last_tool_at=last_active,
    )


class TestPresence:
    def test_absent_user_is_falsy_with_no_repo(self) -> None:
        presence = Presence.of([_session("repo-b", _NOW, user="kai")], "eric")
        assert not presence
        assert presence.mailbox_repo("repo-a") == ""

    def test_local_repo_wins(self) -> None:
        sessions = [_session("repo-b", _NOW), _session("repo-a", _NOW)]
        assert Presence.of(sessions, "eric").mailbox_repo("repo-a") == "repo-a"

    def test_live_repo_beats_stale_local_repo(self) -> None:
        sessions = [_session("repo-a", _STALE), _session("repo-b", _NOW)]
        assert Presence.of(sessions, "eric").mailbox_repo("repo-a") == "repo-b"

    def test_most_recently_active_wins_when_none_live(self) -> None:
        sessions = [
            _session("repo-b", _STALE),
            _session("repo-c", _STALE + timedelta(hours=1)),
        ]
        assert Presence.of(sessions, "eric").mailbox_repo("repo-a") == "repo-c"

    def test_tie_goes_to_first_repo_by_name(self) -> None:
        sessions = [_session("repo-c", _STALE), _session("repo-b", _STALE)]
        assert Presence.of(sessions, "eric").mailbox_repo("repo-a") == "repo-b"

    def test_latest_tool_call_beats_fresher_heartbeat(self) -> None:
        idle = UserSession(
            user="eric", tty="b", repo="repo-b", last_active=_NOW, last_tool_at=_STALE
        )
        used = UserSession(
            user="eric",
            tty="c",
            repo="repo-c",
            last_active=_NOW - timedelta(seconds=30),
            last_tool_at=_NOW - timedelta(minutes=1),
        )
        assert Presence.of([idle, used], "eric").mailbox_repo("repo-a") == "repo-c"
