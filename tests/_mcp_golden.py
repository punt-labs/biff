"""The text every MCP tool returns, as the display pipeline receives it.

``plugin/hooks/suppress-output.sh`` parses a tool's text into the panel line and
the model's context, so the text is a contract.  :data:`STEPS` is one scripted
conversation between two users; each step names the tool, its arguments and the
exact text it returned when this table was written.  Only what the clock
decides (dates, minutes left, idle time) is replaced by a placeholder, step by
step, with the patterns in :data:`VOLATILE`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from biff.models import SessionEvent
from biff.nostr_directory import NostrDirectory
from biff.relay import LocalRelay

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


__all__ = [
    "STEPS",
    "VOLATILE",
    "HistoryRelay",
    "Step",
    "differences",
    "isolate_machine",
    "last_table",
    "normalise",
]

type User = Literal["kai", "eric"]

type Replacement = Callable[[re.Match[str]], str]


def _within(low: int, high: int, token: str) -> Replacement:
    """Replace a match by *token* only when its ``n`` group is in ``low..high``.

    A value outside the band is left as it is, so the comparison fails on it: a
    wall of ten minutes must not pass for a wall of an hour.
    """

    def replace(match: re.Match[str]) -> str:
        if low <= int(match["n"]) <= high:
            return match.expand(token)
        return match[0]

    return replace


VOLATILE: Final[dict[str, tuple[re.Pattern[str], Replacement]]] = {
    "date": (
        re.compile(r"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}"),
        lambda _match: "<DATE>",
    ),
    "idle": (
        re.compile(r"idle 0:(?P<n>\d{2})"),
        _within(0, 1, "idle <0:00-0:01>"),
    ),
    "minutes_left": (
        re.compile(r"\((?P<n>\d+)m(?P<rest> remaining)?\)"),
        _within(58, 60, r"(<58-60>m\g<rest>)"),
    ),
    "idle_column": (
        re.compile(r"(?<=_test-integration  )(?P<n>\d+)m"),
        _within(0, 1, "<0-1>m"),
    ),
}

_X = "x" * 100
_X_WRAPPED = f"{'x' * 77}\n   {'x' * 23}"


@dataclass(frozen=True)
class Step:
    """One tool call and the text it must return."""

    user: User
    tool: str
    args: dict[str, object]
    text: str
    volatile: tuple[str, ...] = ()


def normalise(text: str, names: tuple[str, ...]) -> str:
    """Replace the parts of *text* that the clock decides."""
    for name in names:
        pattern, replace = VOLATILE[name]
        text = pattern.sub(replace, text)
    return text


STEPS: Final[tuple[Step, ...]] = (
    Step("eric", "tty", {"name": "erictty"}, "TTY: erictty"),
    Step("kai", "tty", {"name": "kaitty"}, "TTY: kaitty"),
    Step(
        "kai",
        "tty",
        {"name": "bad name!"},
        "Error: Invalid tty name 'bad name!': "
        "only letters, digits, hyphens, and underscores are allowed.",
    ),
    Step(
        "kai", "plan", {"message": "working on golden"}, "Plan:\n   working on golden"
    ),
    Step("kai", "plan", {"message": _X}, f"Plan:\n   {_X_WRAPPED}"),
    Step(
        "kai",
        "plan",
        {"message": "auto one", "source": "auto"},
        f"Plan unchanged (manual):\n   {_X_WRAPPED}",
    ),
    Step(
        "kai",
        "plan",
        {"message": "x" * 600},
        "Plan too long: 600 characters (max 512)",
    ),
    Step("kai", "plan", {"message": ""}, "Plan:\n   "),
    Step("eric", "plan", {"message": "eric plan"}, "Plan:\n   eric plan"),
    Step(
        "kai",
        "who",
        {},
        "▶  NAME          K  REPO               IDLE  S  P  HOST     \n"
        "   kai:kaitty       _test-integration  <0-1>m    +  -  test-host\n"
        "   eric:erictty     _test-integration  <0-1>m    +  +  test-host",
        ("idle_column",),
    ),
    Step("kai", "mesg", {"enabled": False}, "is n"),
    Step(
        "kai",
        "who",
        {},
        "▶  NAME          K  REPO               IDLE  S  P  HOST     \n"
        "   kai:kaitty       _test-integration  <0-1>m    -  -  test-host\n"
        "   eric:erictty     _test-integration  <0-1>m    +  +  test-host",
        ("idle_column",),
    ),
    Step("kai", "mesg", {"enabled": True}, "is y"),
    Step(
        "kai",
        "finger",
        {"user": "eric"},
        "▶  Login: eric                           Messages: on\n"
        "   On since <DATE> (UTC) on erictty, idle <0:00-0:01>\n"
        "   Host: test-host  Dir: /test\n"
        "   Plan:\n"
        "    eric plan",
        ("date", "idle"),
    ),
    Step(
        "kai",
        "finger",
        {"user": "kai"},
        "▶  Login: kai                            Messages: on\n"
        "   On since <DATE> (UTC) on kaitty, idle <0:00-0:01>\n"
        "   Host: test-host  Dir: /test\n"
        "   No Plan.",
        ("date", "idle"),
    ),
    Step("kai", "finger", {"user": "nobody"}, "Login: nobody\nNever logged in."),
    Step("kai", "last", {}, "No session history."),
    Step("kai", "last", {"user": "eric"}, "No session history."),
    Step(
        "kai",
        "write",
        {"to": "eric:ghosttty", "message": "hi"},
        "ghosttty has no active session; delivered to eric's mailbox in "
        "_test-integration (their next session there will see it).",
    ),
    Step(
        "kai",
        "write",
        {"to": "eric:erictty", "message": "hello eric"},
        "Message sent to eric:erictty.",
    ),
    Step("kai", "write", {"to": "ghost", "message": "hi"}, "Message sent to ghost."),
    Step(
        "kai", "write", {"to": "eric", "message": "broadcast"}, "Message sent to eric."
    ),
    Step(
        "kai",
        "write",
        {"to": "a:b:c", "message": "hi"},
        "user a not found in visible repos",
    ),
    Step(
        "kai",
        "write",
        {"to": "", "message": "hi"},
        "Empty user: give a user or user:tty to write to.",
    ),
    Step(
        "eric",
        "read_messages",
        {},
        "▶  3 new messages\n"
        "   FROM        DATE              MESSAGE"
        "                                        \n"
        f"   kai:kaitty  <DATE>  {'hi':<47}\n"
        f"   kai:kaitty  <DATE>  {'hello eric':<47}\n"
        f"   kai:kaitty  <DATE>  {'broadcast':<47}",
        ("date",),
    ),
    Step("eric", "read_messages", {}, "No new messages."),
    Step("kai", "wall", {}, "No active wall."),
    Step(
        "kai",
        "wall",
        {"message": "freeze now", "duration": "1h"},
        "Wall posted (<58-60>m):\n   freeze now",
        ("minutes_left",),
    ),
    Step(
        "kai",
        "wall",
        {},
        "▶  WALL from kai (kaitty) (<58-60>m remaining)\n   freeze now",
        ("minutes_left",),
    ),
    Step(
        "kai",
        "wall",
        {"message": "m", "duration": "zz"},
        "Unrecognized duration 'zz'. Use 30m, 2h, 1d, 3d.",
    ),
    Step(
        "kai",
        "wall",
        {"message": "m", "repo": "nope/none"},
        "Repo 'nope/none' is not in your visible repos.",
    ),
    Step("kai", "wall", {"clear": True}, "Wall cleared."),
    Step(
        "kai",
        "set_poll_interval",
        {"interval": "5m"},
        "Poll interval set to 5m (300.0s). Mail nudges start now; "
        "the server reads the cadence after a restart.",
    ),
    Step(
        "kai",
        "set_poll_interval",
        {"interval": "bogus"},
        "Invalid interval: bogus. "
        "Use {N}s, {N}m or {N}h format (e.g. 2s, 5m, 1h), or n to disable.",
    ),
    Step(
        "kai",
        "set_poll_interval",
        {"interval": "n"},
        "Polling is off. Biff's server runs no timer either way: messages, walls "
        "and talk still arrive by NATS push or, on the local relay, by a file "
        "watch. Mail nudges stop now.",
    ),
    Step("kai", "get_poll_status", {}, "Polling: active, interval=2s (2s)"),
    Step("kai", "talk", {"to": "eric"}, "Talk requires a NATS relay connection."),
    Step("kai", "talk_read", {}, "Talk requires a NATS relay connection."),
    Step(
        "kai",
        "talk_listen",
        {"timeout": 1},
        "Talk requires a NATS relay connection.",
    ),
    Step("kai", "talk_end", {}, "No active talk session."),
    Step("kai", "biff", {"action": "enable"}, "Error: not in a git repository."),
    Step(
        "kai",
        "biff_relay",
        {"relay_type": "local"},
        "Error: not in a git repository.",
    ),
)


class HistoryRelay(LocalRelay):
    """A local relay that keeps session history, as the NATS relay does.

    ``LocalRelay`` persists none, so ``last`` over it always says there is no
    history; a table with rows needs a relay that remembers.  Events come back
    most recent first, as ``NatsRelay.get_wtmp`` returns them
    (``nats_relay.py``, "most recent first").

    The server stamps its own session's events with the wall clock, which would
    make a table depend on the calendar.  Those events, and only those, take the
    time of *clock* instead; every other event keeps the time it came with.
    """

    def __init__(self, data_dir: Path, *, own_key: str, clock: datetime) -> None:
        super().__init__(data_dir)
        self._own_key = own_key
        self._clock = clock
        self._events: list[SessionEvent] = []

    async def append_wtmp(self, event: SessionEvent) -> None:
        """Remember *event*, its time the injected clock's when it is the server's."""
        if event.session_key == self._own_key:
            event = event.model_copy(update={"timestamp": self._clock})
        self._events.append(event)

    async def get_wtmp(
        self, *, user: str | None = None, count: int = 25
    ) -> list[SessionEvent]:
        """Return up to *count* events, most recent first."""
        wanted = [e for e in self._events if user is None or e.user == user]
        return sorted(wanted, key=lambda e: e.timestamp, reverse=True)[:count]

    async def seed_other_sessions(self) -> None:
        """Add a finished session and one that is gone, both from Nov 20 2026.

        Seed after the server attached: at attach it closes every login that has
        no logout and no live session, so an earlier ghost would be closed.
        """
        at = datetime(2026, 11, 20, tzinfo=UTC)
        for key, kind, user, tty, when in (
            ("eric:e1", "login", "eric", "tty1", at.replace(hour=9)),
            ("eric:e1", "logout", "eric", "tty1", at.replace(hour=10, minute=30)),
            ("ghost:g1", "login", "ghost", "tty2", at.replace(hour=8)),
        ):
            await self.append_wtmp(
                SessionEvent(
                    session_key=key,
                    event=kind,
                    user=user,
                    tty=key.partition(":")[2],
                    tty_name=tty,
                    hostname="test-host",
                    repo="_test-integration",
                    timestamp=when,
                )
            )


def isolate_machine(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Keep a golden run off the machine it runs on.

    A plan writes a marker under the Biff home, and ``who`` looks each user's
    published key up in ethos or the Biff home; neither may reach the person
    who runs the tests, or the text would depend on them.  The home is private to
    *root*, and the key directory is an empty file there with no ethos behind it.
    """
    monkeypatch.setenv("HOME", str(root / "home"))

    def empty_directory(
        cls: type[NostrDirectory], _repo_root: Path | None
    ) -> NostrDirectory:
        return cls(None, root / "nostr.yaml")

    monkeypatch.setattr(NostrDirectory, "for_repo", classmethod(empty_directory))


def differences(steps: tuple[Step, ...], actual: list[str]) -> list[str]:
    """Name each step whose normalised text is not its golden text."""
    return [
        f"{step.user} {step.tool} {step.args}:\n  want {step.text!r}\n  got  {got!r}"
        for step, text in zip(steps, actual, strict=True)
        if (got := normalise(text, step.volatile)) != step.text
    ]


_LAST_HEADER = (
    "▶  NAME        REPO               HOST       LOGIN             "
    "LOGOUT            DURATION"
)
_LAST_ERIC = (
    "   eric:tty1   _test-integration  test-host  Fri Nov 20 09:00  "
    "Fri Nov 20 10:30  1:30    "
)
_LAST_GHOST = (
    "   ghost:tty2  _test-integration  test-host  Fri Nov 20 08:00  "
    "gone              -       "
)


def last_table(login: str, rank: int) -> str:
    """Return the ``last`` table after :meth:`HistoryRelay.seed_other_sessions`.

    The server's own session logged in at *login* (``Tue Dec 01 09:15``), and
    that row stands at index *rank* among the three, newest login first.
    """
    live = (
        f"   kai:tty1    _test-integration  test-host  {login}  "
        "still logged in   -       "
    )
    rows = [_LAST_ERIC, _LAST_GHOST]
    rows.insert(rank, live)
    return "\n".join([_LAST_HEADER, *rows])
