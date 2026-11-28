"""What a stop, and the start after it, do with mail when something goes wrong."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.held_mail import HeldMail
from biff.daemon.outbox import Outbox
from biff.daemon.server import BiffDaemon
from biff.daemon.spill_file import Held, Spilled, SpillFile
from biff.models import Message
from biff.run_dir import RunDir
from tests.test_daemon.conftest import CountingFactory, FakeResolver
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from biff.daemon.sessions import Session
    from tests.test_daemon.conftest import Harness


def _spill_path(harness: Harness) -> Path:
    return harness.sessions.root.parent / "handback-spill.json"


async def _start(harness: Harness, tmp_path: Path) -> BiffDaemon:
    """Start a daemon over the harness's relays and session store."""
    factory = CountingFactory()
    factory.relay_type = ConsumingRelay
    daemon = BiffDaemon(
        run_dir=RunDir(tmp_path / "run-restarted"),
        port=0,
        sessions=harness.sessions,
        resolver=FakeResolver(harness.relays_dir),
        relay_factory=factory,
        request_timeout=0.5,
    )
    await daemon.start()
    return daemon


async def _read(daemon: BiffDaemon, run_dir: RunDir) -> list[str]:
    client = await DaemonClient.connect(
        run_dir=run_dir,
        port=daemon.port,
        identity=ClientIdentity(
            client="cli", session_id="s-kai", cwd="/work/repo", user="kai"
        ),
    )
    try:
        reply = await client.request("read")
    finally:
        await client.close()
    data = cast("dict[str, list[dict[str, str]]]", reply)["data"]
    return sorted(row["body"] for row in data)


def _message(body: str) -> Message:
    return Message(from_user="eric", to_user="kai", body=body)


def _bodies(messages: Sequence[Message]) -> list[str]:
    return sorted(m.body for m in messages)


async def test_a_bad_spill_file_at_start_is_set_aside_and_never_overwritten(
    harness: Harness, tmp_path: Path
) -> None:
    await harness.daemon.stop()
    spill = _spill_path(harness)
    spill.parent.mkdir(parents=True, exist_ok=True)
    for text in ("{first", "{second"):
        spill.write_text(text)
        daemon = await _start(harness, tmp_path)
        await daemon.stop()

    kept = sorted(
        p.read_text()
        for p in spill.parent.glob("handback-spill*")
        if p.read_text() in ("{first", "{second")
    )
    assert kept == ["{first", "{second"]


async def test_a_spill_that_cannot_be_read_never_stops_the_start(
    harness: Harness, tmp_path: Path
) -> None:
    await harness.daemon.stop()
    spill = _spill_path(harness)
    spill.mkdir(parents=True)  # reading it raises, an OSError

    daemon = await _start(harness, tmp_path)  # must not raise
    await daemon.stop()


async def test_an_error_in_the_hand_back_still_spills_the_mail_it_did_not_file(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.factory.relay_type = ConsumingRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="m1")
    await kai.request_held("read")  # m1 is held, unconfirmed

    async def failing(_session: Session) -> None:
        msg = "a bug in the hand-back"
        raise RuntimeError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(HeldMail, "refile", staticmethod(failing))
        with pytest.raises(RuntimeError, match="a bug in the hand-back"):
            await harness.daemon.stop()  # the first failure still propagates

    daemon = await _start(harness, tmp_path)
    try:
        assert await _read(daemon, RunDir(tmp_path / "run-restarted")) == ["m1"]
    finally:
        await daemon.stop()


async def test_a_sealed_outbox_spills_only_what_it_does_not_already_hold() -> None:
    outbox = Outbox(retains=True)
    held, late = _message("held"), _message("late")
    outbox.merge([held])
    spilled: list[Message] = []

    outbox.seal(spilled.extend)
    served = outbox.merge([held, late])  # a read merges what it fetched before too

    assert (_bodies(spilled), _bodies(outbox.held()), served) == (
        ["late"],
        ["held"],
        [],
    )


async def test_the_status_counts_the_spilled_mail_that_could_not_be_filed(
    harness: Harness, tmp_path: Path
) -> None:
    await harness.daemon.stop()
    stuck = Spilled(
        session_id="bad id!",  # no relay can be found for it
        agent=False,
        cwd="/work/repo",
        user="kai",
        held=(Held(message=_message("stuck")),),
    )
    fallback = tmp_path / "run-restarted" / "handback-spill.json"
    SpillFile(_spill_path(harness), fallback).write({stuck.key: stuck})

    daemon = await _start(harness, tmp_path)
    try:
        assert daemon.status().spilled == 1
    finally:
        await daemon.stop()
