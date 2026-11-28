"""A daemon that stops while a read is fetching gives the mail back to the relay."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.server import BiffDaemon
from biff.run_dir import RunDir
from tests.test_daemon.conftest import CountingFactory, FakeResolver
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from biff.models import Message
    from tests.test_daemon.conftest import Harness

_WAIT = 3.0


class FetchingRelay(ConsumingRelay):
    """A consuming relay whose broadcast fetch says it took the mail, then stalls."""

    consumed = asyncio.Event()
    delay = 0.3

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        unread = await super().fetch_user_inbox(user)
        type(self).consumed.set()
        await asyncio.sleep(type(self).delay)
        return unread


def _bodies(result: object) -> list[str]:
    rows = cast("list[dict[str, str]]", cast("dict[str, object]", result)["data"])
    return [row["body"] for row in rows]


async def test_mail_a_read_took_while_the_daemon_stopped_is_there_after_a_restart(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(FetchingRelay, "consumed", asyncio.Event())
    harness.factory.relay_type = FetchingRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="do not lose me")
    reading = asyncio.create_task(kai.request("read"))
    await asyncio.wait_for(FetchingRelay.consumed.wait(), _WAIT)

    await harness.daemon.stop()  # the relay has given the mail up; nothing replied yet

    await asyncio.gather(reading, return_exceptions=True)
    factory = CountingFactory()
    factory.relay_type = ConsumingRelay
    run_dir = RunDir(tmp_path / "run-restarted")
    restarted = BiffDaemon(
        run_dir=run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=FakeResolver(harness.relays_dir),
        relay_factory=factory,
        request_timeout=0.5,
    )
    await restarted.start()
    try:
        client = await DaemonClient.connect(
            run_dir=run_dir,
            port=restarted.port,
            identity=ClientIdentity(
                client="cli", session_id="s-kai", cwd="/work/repo", user="kai"
            ),
        )
        try:
            assert _bodies(await client.request("read")) == ["do not lose me"]
        finally:
            await client.close()
    finally:
        await restarted.stop()
