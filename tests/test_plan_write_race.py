"""An auto plan never overwrites a manual plan that lands between its check and write.

The check and the write are one compare-and-set on the row's revision: a manual
plan saved after the auto write read the row makes the write fail, and the retry
reads the manual plan and keeps it (``presence-heartbeat.tex``, ``PlanWrite``
and ``PlanRefused``).  The interleaving is forced by the key-value bucket, not
by timing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.cli_context import CliContext
from biff.commands.plan_source import PlanWrite
from biff.models import BiffConfig, UserSession
from tests.test_nats_heartbeat_race import KEY, _relay, _RevisionedKV

if TYPE_CHECKING:
    from biff.nats_relay import NatsRelay


def _ctx(relay: NatsRelay) -> CliContext:
    return CliContext(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key=KEY,
        user="kai",
        tty="abc123",
    )


def _manual(row: UserSession, plan: str) -> UserSession:
    return row.model_copy(update={"plan": plan, "plan_source": "manual"})


@pytest.mark.anyio()
async def test_a_manual_plan_saved_after_the_auto_read_is_kept() -> None:
    row = UserSession(user="kai", tty="abc123")
    kv = _RevisionedKV(row)
    kv.after_get = lambda store: store.save(_manual(row, "mine"))

    result = await PlanWrite(_ctx(_relay(kv)), "hook plan", "auto").run()

    assert (kv.row().plan, kv.row().plan_source) == ("mine", "manual")
    assert result.text == "Plan unchanged (manual):\n   mine"


@pytest.mark.anyio()
async def test_an_auto_plan_with_no_competitor_is_written_in_one_update() -> None:
    kv = _RevisionedKV(UserSession(user="kai", tty="abc123"))

    await PlanWrite(_ctx(_relay(kv)), "hook plan", "auto").run()

    assert (kv.row().plan, kv.row().plan_source) == ("hook plan", "auto")
    assert kv.update_attempts == 1


@pytest.mark.anyio()
async def test_a_manual_plan_saved_after_a_manual_read_still_lands() -> None:
    row = UserSession(user="kai", tty="abc123")
    kv = _RevisionedKV(row)
    kv.after_get = lambda store: store.save(_manual(row, "theirs"))

    await PlanWrite(_ctx(_relay(kv)), "mine", "manual").run()

    assert (kv.row().plan, kv.row().plan_source) == ("mine", "manual")


@pytest.mark.anyio()
async def test_a_write_that_loses_every_race_raises_rather_than_overwrite() -> None:
    row = UserSession(user="kai", tty="abc123")
    kv = _RevisionedKV(row)
    kv.after_every_get = lambda store: store.save(
        store.row().model_copy(update={"hostname": f"h{store.update_attempts}"})
    )

    with pytest.raises(ConnectionError, match="changed concurrently"):
        await PlanWrite(_ctx(_relay(kv)), "hook plan", "auto").run()

    assert kv.row().plan == ""
