"""The ``biff`` command runs as a client of biffd, holding no relay of its own."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, cast

import pytest

from biff.cli_daemon import DaemonCommand
from biff.cli_session_store import CliSessionStore

if TYPE_CHECKING:
    from pathlib import Path

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


def _command(harness: Harness, ref: str | None = None) -> DaemonCommand:
    return DaemonCommand(user_override="kai", session_ref=ref, run_dir=harness.run_dir)


async def test_a_command_prints_the_text_biffd_rendered(
    harness: Harness, repo: Path
) -> None:
    result = await _command(harness).call("who")
    assert (result.error, "kai" in result.text) == (False, True)


async def test_consecutive_commands_share_one_session(
    harness: Harness, repo: Path
) -> None:
    first = await _command(harness).call("status")
    second = await _command(harness).call("status")
    assert first.json_data == second.json_data


async def test_a_new_session_records_the_tty_name_biffd_claimed(
    harness: Harness, repo: Path
) -> None:
    await _command(harness).call("who")
    (record,) = CliSessionStore.default().list()
    assert record.tty != "pending"


async def test_plan_syncs_the_marker_in_the_calling_repository(
    harness: Harness, repo: Path
) -> None:
    from biff.markers import has_plan_marker

    command = _command(harness)
    await command.plan("ship biffd")
    (record,) = CliSessionStore.default().list()
    assert has_plan_marker(str(repo), record.session_id)


async def test_tty_renames_the_stored_record(harness: Harness, repo: Path) -> None:
    command = _command(harness)
    result = await command.tty("review")
    (record,) = CliSessionStore.default().list()
    assert (result.error, record.tty) == (False, "review")


async def test_a_refused_tty_name_leaves_the_record_alone(
    harness: Harness, repo: Path
) -> None:
    result = await _command(harness).tty("not a valid name!")
    (record,) = CliSessionStore.default().list()
    assert (result.error, record.tty != "not a valid name!") == (True, True)


async def test_an_unknown_session_name_is_refused(harness: Harness, repo: Path) -> None:
    with pytest.raises(ValueError, match="unknown session"):
        await _command(harness, ref="nosuch").call("who")


async def test_a_later_command_sees_what_an_earlier_one_set(
    harness: Harness, repo: Path
) -> None:
    await _command(harness).call("plan", message="held")
    rows = cast("list[dict[str, str]]", (await _command(harness).call("who")).json_data)
    assert {row["user"]: row["plan"] for row in rows}["kai"] == "held"
