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
from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["STEPS", "VOLATILE", "Step", "normalise"]

type User = Literal["kai", "eric"]

VOLATILE: Final[dict[str, tuple[re.Pattern[str], str]]] = {
    "date": (re.compile(r"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}"), "<DATE>"),
    "idle": (re.compile(r"idle \d+:\d{2}"), "idle <H:MM>"),
    "minutes_left": (re.compile(r"\((\d+)m( remaining)?\)"), r"(<N>m\2)"),
    "idle_column": (re.compile(r"(?<=_test-integration  )\d+m"), "<N>m"),
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
        pattern, token = VOLATILE[name]
        text = pattern.sub(token, text)
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
        "   kai:kaitty       _test-integration  <N>m    +  -  test-host\n"
        "   eric:erictty     _test-integration  <N>m    +  +  test-host",
        ("idle_column",),
    ),
    Step("kai", "mesg", {"enabled": False}, "is n"),
    Step(
        "kai",
        "who",
        {},
        "▶  NAME          K  REPO               IDLE  S  P  HOST     \n"
        "   kai:kaitty       _test-integration  <N>m    -  -  test-host\n"
        "   eric:erictty     _test-integration  <N>m    +  +  test-host",
        ("idle_column",),
    ),
    Step("kai", "mesg", {"enabled": True}, "is y"),
    Step(
        "kai",
        "finger",
        {"user": "eric"},
        "▶  Login: eric                           Messages: on\n"
        "   On since <DATE> (UTC) on erictty, idle <H:MM>\n"
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
        "   On since <DATE> (UTC) on kaitty, idle <H:MM>\n"
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
        "Wall posted (<N>m):\n   freeze now",
        ("minutes_left",),
    ),
    Step(
        "kai",
        "wall",
        {},
        "▶  WALL from kai (kaitty) (<N>m remaining)\n   freeze now",
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
