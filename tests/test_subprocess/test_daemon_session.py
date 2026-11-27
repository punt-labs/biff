"""The real daemon process serves a real client against a real repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ClientIdentity, DaemonClient

if TYPE_CHECKING:
    from pathlib import Path

    from biff.run_dir import RunDir

pytestmark = pytest.mark.subprocess


async def test_two_sessions_share_a_repository_through_the_real_daemon(
    run_dir: RunDir, repo: Path
) -> None:
    def identity(session_id: str, user: str) -> ClientIdentity:
        return ClientIdentity(
            client="cli", session_id=session_id, cwd=str(repo), user=user
        )

    kai = await DaemonClient.connect(run_dir=run_dir, identity=identity("s-kai", "kai"))
    eric = await DaemonClient.connect(
        run_dir=run_dir, identity=identity("s-eric", "eric")
    )
    try:
        await kai.request("plan", message="end to end")
        who = cast("dict[str, list[dict[str, str]]]", await eric.request("who"))
        assert {row["user"]: row["plan"] for row in who["data"]}["kai"] == "end to end"
    finally:
        await kai.close()
        await eric.close()
