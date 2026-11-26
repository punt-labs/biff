"""Mail is marked read only after the reply that carries it has been sent."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from biff.daemon.connection import ConnectionHandler

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness


def _bodies(result: object) -> list[str]:
    data = cast("list[dict[str, str]]", cast("dict[str, object]", result)["data"])
    return [row["body"] for row in data]


def _more(result: object) -> int:
    return cast("int", cast("dict[str, object]", result).get("more", 0))


async def test_a_reply_that_cannot_be_sent_does_not_consume_the_mail(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="do not lose me")

    async def failing_send(_ws: object, _frame: object) -> bool:
        return False  # the connection went away before the reply left

    with monkeypatch.context() as patch:
        patch.setattr(ConnectionHandler, "_send", staticmethod(failing_send))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(kai.request("read"), 0.3)
    again = await harness.connect("s-kai-2", "kai")
    assert "do not lose me" in _bodies(await again.request("read"))


async def test_a_sent_reply_consumes_the_mail_it_carried(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="once only")
    assert _bodies(await kai.request("read")) == ["once only"]
    assert _bodies(await kai.request("read")) == []


async def test_an_oversized_inbox_is_delivered_in_batches_with_none_lost(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    sent = [f"message {n:03d} " + "x" * 200 for n in range(30)]
    for body in sent:
        await eric.request("write", to="kai", message=body)
    received: list[str] = []
    for _ in range(30):
        result = await kai.request("read")
        received += _bodies(result)
        if not _more(result):
            break
    assert received == sent


async def test_a_batch_that_leaves_mail_behind_says_how_much(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    for n in range(30):
        await eric.request("write", to="kai", message=f"m{n:03d} " + "x" * 200)
    first = await kai.request("read")
    assert 0 < len(_bodies(first)) < 30
    assert _more(first) == 30 - len(_bodies(first))


async def test_a_batch_is_never_empty_when_mail_waits(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="y" * 400)
    assert len(_bodies(await kai.request("read"))) == 1
