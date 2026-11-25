"""Tests for poll_config MCP tools — set_poll_interval / get_poll_status."""

from __future__ import annotations

from pathlib import Path

from fastmcp.tools.function_tool import FunctionTool

from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import ServerState
from tests._state import create_state

_TEST_REPO = "_test-server"


def _make_state(tmp_path: Path, *, poll_interval: float = 2.0) -> ServerState:
    config = BiffConfig(user="kai", repo_name=_TEST_REPO, poll_interval=poll_interval)
    return create_state(
        config,
        tmp_path,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
        repo_root=tmp_path,
    )


def _make_nats_state(tmp_path: Path, *, poll_interval: float = 2.0) -> ServerState:
    """A state whose relay is NATS-backed (never dials — no server needed).

    ``set_poll_interval``'s push-oriented description/response text is only
    accurate for a NATS-backed relay; tests that
    assert on that text must use this, not the default ``_make_state``
    (filesystem-backed ``LocalRelay``, which has no push mechanism at all).
    """
    state = _make_state(tmp_path, poll_interval=poll_interval)
    relay = NatsRelay(url="nats://localhost:4222", stream_prefix="biff-dev")
    object.__setattr__(state, "relay", relay)
    return state


async def _get_tool_fn(state: ServerState, tool_name: str):
    mcp = create_server(state)
    tool = await mcp.get_tool(tool_name)
    assert tool is not None
    assert isinstance(tool, FunctionTool)
    return tool.fn


class TestSetPollInterval:
    async def test_set_valid_seconds(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="5s")
        assert "5s" in result
        assert "nudges start now" in result

    async def test_set_valid_minutes(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="2m")
        assert "2m" in result
        assert "after a restart" in result

    async def test_disable(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="n")
        assert "off" in result.lower()
        assert "nudges stop now" in result

    async def test_disable_response_says_arrivals_do_not_need_polling(
        self, tmp_path: Path
    ) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="n")
        assert "off" in result.lower()
        assert "push" in result.lower()
        assert "file watch" in result.lower()
        assert "no timer" in result.lower()

    async def test_invalid_interval(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        result = await fn(interval="banana")
        assert "Invalid" in result

    async def test_persists_to_local_yaml(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        # Create the config dir so write succeeds
        config_dir = tmp_path / ".punt-labs" / "biff"
        config_dir.mkdir(parents=True)
        fn = await _get_tool_fn(state, "set_poll_interval")
        await fn(interval="10s")
        import yaml

        local_yaml = config_dir / "config.local.yaml"
        assert local_yaml.exists()
        data = yaml.safe_load(local_yaml.read_text())
        assert data["poll_interval"] == 10.0

    async def test_ensures_gitignore(self, tmp_path: Path) -> None:
        """Persisting a poll interval keeps config.local.yaml out of git."""
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "set_poll_interval")
        await fn(interval="10s")
        gitignore = tmp_path / ".punt-labs" / "biff" / ".gitignore"
        assert gitignore.exists()
        assert "config.local.yaml" in gitignore.read_text()


class TestSetPollIntervalDescription:
    """The description says the server runs no timer for the cadence."""

    async def _description(self, state: ServerState) -> str:
        tool = await create_server(state).get_tool("set_poll_interval")
        assert tool is not None
        return tool.description or ""

    async def test_states_the_server_runs_no_timer(self, tmp_path: Path) -> None:
        desc = await self._description(_make_state(tmp_path))
        assert "no timer" in desc.lower()

    async def test_names_both_arrival_paths(self, tmp_path: Path) -> None:
        for state in (_make_nats_state(tmp_path), _make_state(tmp_path)):
            desc = await self._description(state)
            assert "push" in desc.lower()
            assert "file watch" in desc.lower()

    async def test_does_not_claim_retired_periodic_work(self, tmp_path: Path) -> None:
        desc = (await self._description(_make_nats_state(tmp_path))).lower()
        for retired in ("backstop", "countdown", "invite expiry", "15x"):
            assert retired not in desc


class TestGetPollStatus:
    async def test_default_interval(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        fn = await _get_tool_fn(state, "get_poll_status")
        result = await fn()
        assert "active" in result.lower()
        assert "2s" in result

    async def test_disabled(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path, poll_interval=0.0)
        fn = await _get_tool_fn(state, "get_poll_status")
        result = await fn()
        assert "disabled" in result.lower()

    async def test_minutes_display(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path, poll_interval=120.0)
        fn = await _get_tool_fn(state, "get_poll_status")
        result = await fn()
        assert "2m" in result

    async def test_no_timeout_line_before_any_attempt(self, tmp_path: Path) -> None:
        # LocalRelay (the default test relay) never appends the line at
        # all; a fresh NatsRelay with zero attempts must not either — an
        # empty "0 timeout(s) / 0 request(s)" line would be noise, not
        # signal.
        state = _make_state(tmp_path)
        relay = NatsRelay(url="nats://localhost:4222", stream_prefix="biff-dev")
        object.__setattr__(state, "relay", relay)
        fn = await _get_tool_fn(state, "get_poll_status")
        result = await fn()
        assert "NATS relay:" not in result

    async def test_timeout_line_reports_cumulative_counts(self, tmp_path: Path) -> None:
        # This line is the server-side record that lets an
        # operator measure the real timeout rate instead of relying on
        # which timeouts happened to be noticed client-side.
        state = _make_state(tmp_path)
        relay = NatsRelay(url="nats://localhost:4222", stream_prefix="biff-dev")
        for _ in range(20):
            relay._health.record_attempt()
        relay._health.record_timeout_attempt()
        relay._health.record_timeout("read_messages", is_connected=True)
        object.__setattr__(state, "relay", relay)
        fn = await _get_tool_fn(state, "get_poll_status")
        result = await fn()
        assert "NATS relay: 1 timeout(s) / 20 request(s) since server start" in result


class TestParseInterval:
    """Unit tests for _parse_interval helper."""

    def test_seconds(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("2s") == 2.0
        assert _parse_interval("30s") == 30.0

    def test_minutes(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("1m") == 60.0
        assert _parse_interval("5m") == 300.0

    def test_hours(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("1h") == 3600.0
        assert _parse_interval("2h") == 7200.0

    def test_disable(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("n") is None

    def test_invalid(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("banana") == -1.0
        assert _parse_interval("") == -1.0

    def test_whitespace_stripped(self) -> None:
        from biff.server.tools.poll_config import _parse_interval

        assert _parse_interval("  5s  ") == 5.0
        assert _parse_interval("  N  ") is None


class TestPollingMarker:
    """``set_poll_interval`` writes the nudge hook's ``.polling`` marker."""

    async def test_setting_an_interval_marks_polling_on(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path, poll_interval=0)
        object.__setattr__(state, "unread_path", tmp_path / "unread.json")
        fn = await _get_tool_fn(state, "set_poll_interval")
        await fn(interval="2m")
        assert (tmp_path / "unread.polling").read_text() == "True"

    async def test_turning_polling_off_marks_it_off(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path, poll_interval=120)
        object.__setattr__(state, "unread_path", tmp_path / "unread.json")
        (tmp_path / "unread.polling").write_text("True")
        fn = await _get_tool_fn(state, "set_poll_interval")
        await fn(interval="n")
        assert (tmp_path / "unread.polling").read_text() == "False"
