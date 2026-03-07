"""Tier 2b: CLI multi-user scenario tests.

Two ``cli_session()`` instances (kai and eric) sharing a local NATS
server.  Tests exercise real NATS paths (JetStream messaging, KV
presence, wtmp events) using ``biff.commands`` pure async functions.

Requires ``nats-server`` on PATH.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biff import commands
from biff.cli_session import CliContext

pytestmark = pytest.mark.nats


class TestPresence:
    """Scenario 1: both users appear in /who, each sees the other."""

    @pytest.mark.anyio()
    async def test_both_visible_in_who(self, kai: CliContext, eric: CliContext) -> None:
        result = await commands.who(kai)
        assert "kai" in result.text
        assert "eric" in result.text

    @pytest.mark.anyio()
    async def test_who_from_either_side(
        self, kai: CliContext, eric: CliContext
    ) -> None:
        result = await commands.who(eric)
        assert "kai" in result.text
        assert "eric" in result.text

    @pytest.mark.anyio()
    async def test_who_json_has_both(self, kai: CliContext, eric: CliContext) -> None:
        result = await commands.who(kai)
        assert result.json_data is not None
        data: list[dict[str, object]] = result.json_data  # type: ignore[assignment]
        users = [s["user"] for s in data]
        assert "kai" in users
        assert "eric" in users

    @pytest.mark.anyio()
    async def test_tty_names_assigned(self, kai: CliContext, eric: CliContext) -> None:
        """Both sessions get auto-assigned ttyN names."""
        assert kai.tty_name.startswith("tty")
        assert eric.tty_name.startswith("tty")
        assert kai.tty_name != eric.tty_name


class TestMessaging:
    """Scenario 2: write → read (POP semantics)."""

    @pytest.mark.anyio()
    async def test_write_and_read(self, kai: CliContext, eric: CliContext) -> None:
        await commands.write(kai, "@eric", "review the PR")
        result = await commands.read(eric)
        assert "review the PR" in result.text

    @pytest.mark.anyio()
    async def test_pop_semantics(self, kai: CliContext, eric: CliContext) -> None:
        """Messages consumed on read — second read returns empty."""
        await commands.write(kai, "@eric", "hello")
        await commands.read(eric)
        result = await commands.read(eric)
        assert "No new messages" in result.text

    @pytest.mark.anyio()
    async def test_bidirectional(self, kai: CliContext, eric: CliContext) -> None:
        await commands.write(kai, "@eric", "from kai")
        await commands.write(eric, "@kai", "from eric")
        kai_inbox = await commands.read(kai)
        eric_inbox = await commands.read(eric)
        assert "from eric" in kai_inbox.text
        assert "from kai" in eric_inbox.text


class TestWall:
    """Scenario 3: wall broadcast visibility."""

    @pytest.mark.anyio()
    async def test_wall_visible_to_both(
        self, kai: CliContext, eric: CliContext
    ) -> None:
        await commands.wall(kai, "release freeze", "1h", clear=False)
        result = await commands.wall(eric, "", "", clear=False)
        assert "release freeze" in result.text

    @pytest.mark.anyio()
    async def test_wall_clear(self, kai: CliContext, eric: CliContext) -> None:
        await commands.wall(kai, "freeze", "1h", clear=False)
        await commands.wall(kai, "", "", clear=True)
        result = await commands.wall(eric, "", "", clear=False)
        assert "No active wall" in result.text


class TestPlanVisibility:
    """Scenario 4: plan → finger."""

    @pytest.mark.anyio()
    async def test_plan_visible_via_finger(
        self, kai: CliContext, eric: CliContext
    ) -> None:
        await commands.plan(kai, "debugging websocket")
        result = await commands.finger(eric, "@kai")
        assert "debugging websocket" in result.text

    @pytest.mark.anyio()
    async def test_plan_visible_in_who(self, kai: CliContext, eric: CliContext) -> None:
        await commands.plan(kai, "fixing tests")
        result = await commands.who(eric)
        assert "fixing tests" in result.text


class TestSessionCleanup:
    """Scenario 5: session cleanup on exit."""

    @pytest.mark.anyio()
    async def test_session_disappears_after_exit(
        self, kai: CliContext, nats_server: str, tmp_path: Path
    ) -> None:
        """After eric's session exits, kai's /who no longer shows eric."""
        from unittest.mock import patch

        from biff.cli_session import cli_session
        from biff.config import ResolvedConfig
        from biff.models import BiffConfig

        resolved = ResolvedConfig(
            config=BiffConfig(
                user="eric",
                repo_name="_test-cli-multi",
                relay_url=nats_server,
            ),
            data_dir=tmp_path / "eric2",
            repo_root=tmp_path,
        )

        # Eric connects and is visible.
        with patch("biff.cli_session.load_config", return_value=resolved):
            async with cli_session() as _eric:
                result = await commands.who(kai)
                assert "eric" in result.text

        # Eric disconnected — no longer visible.
        result = await commands.who(kai)
        assert result.json_data is not None
        data: list[dict[str, object]] = result.json_data  # type: ignore[assignment]
        users = [s["user"] for s in data]
        assert "eric" not in users


class TestWtmp:
    """Scenario 6: wtmp login/logout events."""

    @pytest.mark.anyio()
    async def test_login_events_in_last(
        self, kai: CliContext, eric: CliContext
    ) -> None:
        result = await commands.last(kai, "", 25)
        assert "kai" in result.text
        assert "eric" in result.text

    @pytest.mark.anyio()
    async def test_last_shows_still_logged_in(
        self, kai: CliContext, eric: CliContext
    ) -> None:
        result = await commands.last(kai, "", 25)
        assert "still logged in" in result.text


class TestMesg:
    """Scenario 7: mesg off — write still delivers to inbox."""

    @pytest.mark.anyio()
    async def test_mesg_off_still_receives(
        self, kai: CliContext, eric: CliContext
    ) -> None:
        await commands.mesg(eric, "off")
        await commands.write(kai, "@eric", "urgent message")
        result = await commands.read(eric)
        assert "urgent message" in result.text

    @pytest.mark.anyio()
    async def test_mesg_status_visible(self, kai: CliContext, eric: CliContext) -> None:
        await commands.mesg(eric, "off")
        result = await commands.finger(kai, "@eric")
        assert "messages: off" in result.text.lower()
