"""The ``biff`` command runs as a client of biffd, holding no relay of its own."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, cast

import pytest
from typer.testing import CliRunner

from biff.__main__ import app
from biff.cli_daemon import DaemonCommand
from biff.cli_daemon_wire import UnreadableReplyError
from biff.cli_session_store import CliSessionStore
from biff.client.connection import DaemonUnreachableError
from biff.client.nudge import DaemonNudge
from biff.commands import CommandResult
from biff.daemon import methods as methods_module
from biff.daemon.methods import Methods, Outcome
from biff.markers import has_plan_marker
from biff.plugins.claude_code.hook import handle_pre_tool_use
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result
    from pydantic import JsonValue

    from biff.daemon.sessions import Session
    from biff.supervision.starter import StartOutcome
    from tests.test_daemon.conftest import Harness


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository the command runs in, with its own home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    path = tmp_path / "work" / "demo"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    monkeypatch.chdir(path)
    return path


def _command(
    harness: Harness,
    ref: str | None = None,
    nudge: Callable[[], None] = lambda: None,
) -> DaemonCommand:
    return DaemonCommand(
        user_override="kai", session_ref=ref, run_dir=harness.run_dir, nudge=nudge
    )


async def _text(command: DaemonCommand, method: str, **params: JsonValue) -> str:
    """Run *method*, show the result, confirm it, and return the text shown."""
    answer = await command.call(method, **params)
    shown: list[str] = []
    await answer.deliver(lambda result: shown.append(result.text))
    return shown[0]


async def test_a_command_prints_the_text_biffd_rendered(
    harness: Harness, repo: Path
) -> None:
    answer = await _command(harness).call("who")
    assert (answer.result.error, "kai" in answer.result.text) == (False, True)
    await answer.abandon()


async def test_consecutive_commands_share_one_session(
    harness: Harness, repo: Path
) -> None:
    assert await _text(_command(harness), "status") == await _text(
        _command(harness), "status"
    )


async def test_a_new_session_records_the_tty_name_biffd_claimed(
    harness: Harness, repo: Path
) -> None:
    await _text(_command(harness), "who")
    (record,) = CliSessionStore.default().list()
    assert record.tty != "pending"


async def test_a_command_that_never_reaches_biffd_leaves_no_session_behind(
    harness: Harness, repo: Path
) -> None:
    def down() -> None:
        raise DaemonUnreachableError("biffd is not answering")

    with pytest.raises(DaemonUnreachableError):
        await _command(harness, nudge=down).call("who")
    assert CliSessionStore.default().list() == ()
    await _text(_command(harness), "who")  # biffd is up now
    (record,) = CliSessionStore.default().list()
    assert record.tty == "tty1"


async def test_plan_syncs_the_marker_in_the_calling_repository(
    harness: Harness, repo: Path
) -> None:
    answer = await _command(harness).plan("ship biffd")
    await answer.deliver(lambda result: None)
    (record,) = CliSessionStore.default().list()
    assert has_plan_marker(str(repo), record.session_id)


async def test_the_edit_gate_passes_after_a_cli_plan_under_a_harness_session(
    harness: Harness, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-sess")
    answer = await _command(harness).plan("ship biffd")
    await answer.deliver(lambda result: None)
    gate = handle_pre_tool_use({"session_id": "claude-sess", "cwd": str(repo)})
    assert gate is None  # allowed


async def test_tty_renames_the_stored_record(harness: Harness, repo: Path) -> None:
    answer = await _command(harness).tty("review")
    await answer.deliver(lambda result: None)
    (record,) = CliSessionStore.default().list()
    assert (answer.result.error, record.tty) == (False, "review")


async def test_a_refused_tty_name_leaves_the_record_alone(
    harness: Harness, repo: Path
) -> None:
    answer = await _command(harness).tty("not a valid name!")
    await answer.deliver(lambda result: None)
    (record,) = CliSessionStore.default().list()
    assert (answer.result.error, record.tty) == (True, "tty1")


async def test_a_tty_rename_the_record_refuses_is_undone_in_the_relay(
    harness: Harness, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _text(_command(harness), "who")  # the session exists, named tty1

    def refuse(self: CliSessionStore, session_id: str, tty: str) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(CliSessionStore, "rename", refuse)
        with pytest.raises(OSError, match="disk full"):
            await _command(harness).tty("review")
    status = await _text(_command(harness), "status")
    assert "session: tty1" in status  # the relay name is back
    (record,) = CliSessionStore.default().list()
    assert record.tty == "tty1"


async def test_an_unknown_session_name_is_refused(harness: Harness, repo: Path) -> None:
    with pytest.raises(ValueError, match="unknown session"):
        await _command(harness, ref="nosuch").call("who")


async def test_a_later_command_sees_what_an_earlier_one_set(
    harness: Harness, repo: Path
) -> None:
    await _text(_command(harness), "plan", message="held")
    answer = await _command(harness).call("who")
    rows = cast("list[dict[str, str]]", answer.result.json_data)
    await answer.abandon()
    assert {row["user"]: row["plan"] for row in rows}["kai"] == "held"


def _reshape_replies(
    monkeypatch: pytest.MonkeyPatch, reshape: Callable[[dict[str, JsonValue]], None]
) -> None:
    real = Methods.call

    async def reshaped(
        self: Methods, session: Session, method: str, raw: dict[str, JsonValue]
    ) -> Outcome:
        out = await real(self, session, method, raw)
        result = dict(cast("dict[str, JsonValue]", out.result))
        reshape(result)
        return Outcome(result, out.acknowledge)

    monkeypatch.setattr(methods_module.Methods, "call", reshaped)


async def _leave_mail(harness: Harness, repo: Path) -> None:
    """Make the CLI's session exist and give it one unread message."""
    await _text(_command(harness), "who")
    eric = await harness.connect("s-eric", "eric", cwd=str(repo))
    await eric.request("write", to="kai", message="precious")


async def test_a_reply_from_an_older_biffd_is_refused_before_mail_is_confirmed(
    harness: Harness, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _leave_mail(harness, repo)
    with monkeypatch.context() as patch:

        def drop_text(result: dict[str, JsonValue]) -> None:
            del result["text"]

        _reshape_replies(patch, drop_text)
        with pytest.raises(UnreadableReplyError, match="biff daemon restart"):
            await _command(harness).call("read")
    assert "precious" in await _text(_command(harness), "read")


async def test_a_field_a_newer_biffd_adds_is_ignored(
    harness: Harness, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _leave_mail(harness, repo)

    def add_field(result: dict[str, JsonValue]) -> None:
        result["future"] = 1

    _reshape_replies(monkeypatch, add_field)
    assert "precious" in await _text(_command(harness), "read")


async def test_mail_stays_unread_when_the_result_cannot_be_shown(
    harness: Harness, repo: Path
) -> None:
    await _leave_mail(harness, repo)
    answer = await _command(harness).call("read")

    def closed_pipe(result: CommandResult) -> None:
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        await answer.deliver(closed_pipe)
    assert "precious" in await _text(_command(harness), "read")


async def test_mail_is_read_once_it_has_been_shown(
    harness: Harness, repo: Path
) -> None:
    await _leave_mail(harness, repo)
    assert "precious" in await _text(_command(harness), "read")
    assert await _text(_command(harness), "read") == "No new messages."


async def test_a_backlog_says_how_much_mail_is_left(
    harness: Harness, repo: Path
) -> None:
    await _text(_command(harness), "who")
    eric = await harness.connect("s-eric", "eric", cwd=str(repo))
    for n in range(300):
        await eric.request("write", to="kai", message=f"letter {n:03d} " + "x" * 100)
    answer = await _command(harness).call("read")
    result = answer.result
    await answer.abandon()
    assert "more unread; run biff read again" in result.text
    payload = cast("dict[str, JsonValue]", result.json_data)
    assert cast("int", payload["more"]) > 0


class _Probe:
    """biffd answers once the starter has run, if the starter launched it."""

    def __init__(self, starter: _Starter, comes_up: bool) -> None:
        self._starter = starter
        self._comes_up = comes_up

    def answers(self, timeout: float) -> bool:
        return bool(self._starter.starts) and self._comes_up


class _Starter:
    """A fake service backend that records starts and reports one outcome."""

    def __init__(self, outcome: StartOutcome) -> None:
        self._outcome: StartOutcome = outcome
        self.starts: list[str] = []

    def start(self) -> StartOutcome:
        self.starts.append("start")
        return self._outcome


def _nudge(outcome: StartOutcome, *, comes_up: bool) -> tuple[DaemonNudge, _Starter]:
    starter = _Starter(outcome)
    nudge = DaemonNudge(
        _Probe(starter, comes_up), starter, patience=0.05, interval=0.01
    )
    return nudge, starter


async def test_a_command_with_biffd_down_starts_it_first(
    harness: Harness, repo: Path
) -> None:
    nudge, starter = _nudge("launched", comes_up=True)
    text = await _text(_command(harness, nudge=nudge.ensure_running), "who")
    assert (starter.starts, "kai" in text) == (["start"], True)


def _run_cli(
    nudge: DaemonNudge, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Result:
    command = DaemonCommand(
        user_override="kai",
        session_ref=None,
        run_dir=RunDir(run_dir),
        nudge=nudge.ensure_running,
    )

    def this_command(
        cls: type[DaemonCommand], user: str | None, session: str | None
    ) -> DaemonCommand:
        return command

    monkeypatch.setattr(DaemonCommand, "for_this_process", classmethod(this_command))
    return CliRunner().invoke(app, ["who"])


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        ("not_installed", "biffd is not installed. Run: biff install"),
        (
            "launched",
            (
                "biffd is installed but did not start within 5 seconds. "
                "Run: biff daemon status, then biff doctor"
            ),
        ),
    ],
)
def test_when_biffd_cannot_be_started_the_command_exits_1_with_the_design_message(
    outcome: StartOutcome,
    message: str,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nudge, _ = _nudge(outcome, comes_up=False)
    result = _run_cli(nudge, tmp_path / "run", monkeypatch)
    assert (result.exit_code, message in result.output) == (1, True)
