"""``biff session`` asks biffd for the verb and keeps only the repo pointer."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, cast

import pytest

from biff.cli_daemon import DaemonCommand
from biff.config import load_cli_config
from biff.daemon.session_select import SessionSelector

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_daemon.conftest import Harness


def _command(harness: Harness, repo: Path | None = None) -> DaemonCommand:
    return DaemonCommand(
        user_override="kai",
        session_ref=None,
        run_dir=harness.run_dir,
        nudge=lambda: harness.daemon.port,
        repo=repo,
    )


def _pointed_id() -> str | None:
    """The session id the current repository points at, read as the CLI does."""
    selector = SessionSelector.from_resolved(load_cli_config(user_override="kai"))
    return selector.pointer.read(selector.repo_root)


async def test_session_new_points_the_repo_at_the_new_session(
    harness: Harness, repo: Path
) -> None:
    answer = await _command(harness).session_new("main")
    shown: list[str] = []
    await answer.deliver(lambda result: shown.append(result.text))
    (record,) = (r for r in harness.sessions.list() if r.tty == "main")
    assert (shown, _pointed_id()) == (["main"], record.session_id)


async def test_a_refused_session_new_leaves_the_pointer_alone(
    harness: Harness, repo: Path
) -> None:
    first = await _command(harness).session_new("main")
    await first.deliver(lambda result: None)
    before = _pointed_id()
    second = await _command(harness).session_new("main")
    await second.deliver(lambda result: None)
    assert (second.result.error, _pointed_id()) == (True, before)


async def test_session_use_points_the_repo_without_changing_the_session(
    harness: Harness, repo: Path
) -> None:
    for name in ("alpha", "beta"):
        made = await _command(harness).session_new(name)
        await made.deliver(lambda result: None)
    alpha = next(r for r in harness.sessions.list() if r.tty == "alpha")
    harness.sessions.lapse(alpha.session_id)
    answer = await _command(harness).session_use("alpha")
    await answer.deliver(lambda result: None)
    assert (_pointed_id(), harness.sessions.read(alpha.session_id).state) == (
        alpha.session_id,
        "dormant",
    )


async def test_session_use_of_an_unknown_name_is_an_error_and_points_nowhere_new(
    harness: Harness, repo: Path
) -> None:
    made = await _command(harness).session_new("alpha")
    await made.deliver(lambda result: None)
    before = _pointed_id()
    answer = await _command(harness).session_use("nope")
    await answer.deliver(lambda result: None)
    assert (answer.result.text, _pointed_id()) == (
        "Error: unknown session: nope",
        before,
    )


async def test_session_ls_prints_the_daemons_table(
    harness: Harness, repo: Path
) -> None:
    made = await _command(harness).session_new("main")
    await made.deliver(lambda result: None)
    answer = await _command(harness).session_ls(include_ended=False)
    text = answer.result.text
    await answer.abandon()
    assert text.splitlines()[0].startswith("NAME")
    assert "main" in text


async def test_session_end_ends_the_named_session(harness: Harness, repo: Path) -> None:
    made = await _command(harness).session_new("main")
    await made.deliver(lambda result: None)
    answer = await _command(harness).session_end("main")
    await answer.deliver(lambda result: None)
    (record,) = (r for r in harness.sessions.list() if r.tty == "main")
    assert (answer.result.text, record.state) == ("ended main", "ended")


async def test_a_session_new_in_another_repo_is_made_there(
    harness: Harness, repo: Path, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere" / "test"
    other.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(other)], check=True)  # noqa: S603, S607
    answer = await _command(harness, other).session_new("remote")
    await answer.deliver(lambda result: None)
    (record,) = (r for r in harness.sessions.list() if r.tty == "remote")
    assert record.repo_root == str(other)


async def test_a_session_verb_in_a_fresh_repo_can_take_the_first_name(
    harness: Harness, repo: Path
) -> None:
    answer = await _command(harness).session_new("tty1")
    await answer.deliver(lambda result: None)
    assert (answer.result.error, answer.result.text) == (False, "tty1")


async def test_a_session_verb_leaves_no_session_nobody_asked_for(
    harness: Harness, repo: Path
) -> None:
    made = await _command(harness).session_new("main")
    await made.deliver(lambda result: None)
    live = [r.tty for r in harness.sessions.list() if r.state != "ended"]
    assert live == ["main"]


async def test_session_ls_in_a_fresh_repo_lists_nothing(
    harness: Harness, repo: Path
) -> None:
    answer = await _command(harness).session_ls(include_ended=False)
    text = answer.result.text
    await answer.abandon()
    assert text == "No sessions."


async def test_session_ls_all_does_not_list_the_session_a_verb_made_to_run_as(
    harness: Harness, repo: Path
) -> None:
    made = await _command(harness).session_new("main")
    await made.deliver(lambda result: None)
    answer = await _command(harness).session_ls(include_ended=True)
    rows = cast("list[dict[str, str]]", answer.data)
    await answer.abandon()
    assert [row["name"] for row in rows] == ["main"]


async def test_a_session_cannot_be_renamed_to_an_id_shaped_name(
    harness: Harness, repo: Path
) -> None:
    first = await _command(harness).tty("review")
    await first.deliver(lambda result: None)
    with pytest.raises(ValueError, match="looks like a session id"):
        await _command(harness).tty("cli-0123456789abcdef")
    names = [r.tty for r in harness.sessions.list() if r.state != "ended"]
    assert names == ["review"]
