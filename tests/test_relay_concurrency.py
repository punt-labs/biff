"""Concurrent writers on the local relay never lose each other's updates."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

from biff.models import Message, UserSession
from biff.relay import LocalRelay


def _deliver_many(directory: str, start: int, count: int) -> None:
    import asyncio

    relay = LocalRelay(Path(directory))

    async def run() -> None:
        for n in range(start, start + count):
            await relay.deliver(Message(from_user="kai", to_user="eric", body=f"m{n}"))

    asyncio.run(run())


def _mark_read_loop(directory: str, rounds: int) -> None:
    import asyncio

    relay = LocalRelay(Path(directory))

    async def run() -> None:
        for _ in range(rounds):
            unread = await relay.fetch_user_inbox("eric")
            await relay.mark_read_user_inbox("eric", [m.id for m in unread])

    asyncio.run(run())


async def test_mail_delivered_during_mark_read_is_never_lost(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    writer = ctx.Process(target=_deliver_many, args=(str(tmp_path), 0, 60))
    reader = ctx.Process(target=_mark_read_loop, args=(str(tmp_path), 60))
    writer.start()
    reader.start()
    writer.join(60)
    reader.join(60)
    relay = LocalRelay(tmp_path)
    bodies = {m.body for m in relay._read_inbox_file(tmp_path / "userinbox-eric.jsonl")}
    assert bodies == {f"m{n}" for n in range(60)}


def _heartbeat_loop(directory: str, rounds: int) -> None:
    import asyncio

    relay = LocalRelay(Path(directory))

    async def run() -> None:
        for _ in range(rounds):
            await relay.heartbeat("kai:abc12345")

    asyncio.run(run())


def _plan_loop(directory: str, rounds: int) -> None:
    import asyncio

    relay = LocalRelay(Path(directory))

    async def run() -> None:
        for n in range(rounds):
            await relay.update_session(
                UserSession(user="eric", tty="def67890", plan=f"plan {n}")
            )

    asyncio.run(run())


async def test_a_heartbeat_never_restores_an_older_plan(tmp_path: Path) -> None:
    await LocalRelay(tmp_path).update_session(UserSession(user="kai", tty="abc12345"))
    ctx = multiprocessing.get_context("spawn")
    beat = ctx.Process(target=_heartbeat_loop, args=(str(tmp_path), 80))
    plan = ctx.Process(target=_plan_loop, args=(str(tmp_path), 80))
    beat.start()
    plan.start()
    beat.join(60)
    plan.join(60)
    session = await LocalRelay(tmp_path).get_session("eric:def67890")
    assert session is not None
    assert session.plan == "plan 79"
