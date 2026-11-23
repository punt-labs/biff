"""Channel mode through the real ``biff mcp`` stdio process (DES-087).

A raw JSON-RPC driver, not an SDK client: Claude Code opens with a 2026-07-28
``server/discover`` probe and falls back to ``initialize``, and an SDK client
would hide which of the two the server answered.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Generator, Mapping
from pathlib import Path
from typing import IO, Any

import pytest

pytestmark = pytest.mark.subprocess

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TIMEOUT = 20.0
_METHOD_NOT_FOUND = -32601
_MODERN_META: dict[str, Any] = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "probe", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
_INITIALIZE: dict[str, Any] = {
    "protocolVersion": "2025-11-25",
    "capabilities": {"roots": {}, "elicitation": {}},
    "clientInfo": {"name": "claude-code", "version": "2.1.330"},
}


class _Server:
    """A ``biff mcp`` subprocess speaking newline-delimited JSON-RPC."""

    def __init__(self, tmp_path: Path, *, channel_mode: bool) -> None:
        home = tmp_path / "home"
        home.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["HOME"] = str(home)
        env.pop("BIFF_CHANNEL_MODE", None)
        if channel_mode:
            env["BIFF_CHANNEL_MODE"] = "1"
        self.log = home / ".punt-labs" / "biff" / "logs" / "biff.log"
        self._proc = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "biff",
                "mcp",
                "--user",
                f"chan-{tmp_path.name}".lower(),
                "--data-dir",
                str(data_dir),
                "--relay-url",
                "",
            ],
            cwd=_REPO_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._frames: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0

    def _pump(self) -> None:
        stdout: IO[str] | None = self._proc.stdout
        assert stdout is not None
        for line in stdout:
            self._frames.put(json.loads(line))
        self._frames.put(None)

    def request(
        self, method: str, params: Mapping[str, object]
    ) -> dict[str, Any] | None:
        """Send one request and return its response; ``None`` on end of stream."""
        self._next_id += 1
        self.send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        )
        while (frame := self._frames.get(timeout=_TIMEOUT)) is not None:
            if frame.get("id") == self._next_id:
                return frame
        return None

    def send(self, message: Mapping[str, object]) -> None:
        stdin = self._proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()

    def initialize(self) -> dict[str, Any]:
        response = self.request("initialize", _INITIALIZE)
        assert response is not None
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def close_stdin(self) -> None:
        stdin = self._proc.stdin
        assert stdin is not None
        stdin.close()

    def at_end_of_stream(self) -> bool:
        """Whether stdout reached EOF (the server closed its write stream)."""
        deadline = time.monotonic() + _TIMEOUT
        while time.monotonic() < deadline:
            try:
                if self._frames.get(timeout=0.1) is None:
                    return True
            except queue.Empty:
                continue
        return False

    def wait(self) -> int:
        return self._proc.wait(timeout=_TIMEOUT)

    def kill(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=5)


@pytest.fixture
def channel_server(tmp_path: Path) -> Generator[_Server]:
    server = _Server(tmp_path, channel_mode=True)
    yield server
    server.kill()


@pytest.fixture
def plain_server(tmp_path: Path) -> Generator[_Server]:
    server = _Server(tmp_path, channel_mode=False)
    yield server
    server.kill()


class TestChannelMode:
    """With ``BIFF_CHANNEL_MODE=1`` the server is a handshake-only channel."""

    def test_probe_is_refused_then_initialize_succeeds_with_the_capability(
        self, channel_server: _Server
    ) -> None:
        probe = channel_server.request("server/discover", {"_meta": _MODERN_META})
        assert probe is not None
        assert probe["error"]["code"] == _METHOD_NOT_FOUND

        result = channel_server.initialize()["result"]

        assert result["protocolVersion"] == "2025-11-25"
        assert result["capabilities"]["experimental"] == {"claude/channel": {}}

    def test_initialize_first_also_works(self, channel_server: _Server) -> None:
        result = channel_server.initialize()["result"]

        assert result["capabilities"]["experimental"] == {"claude/channel": {}}

    def test_tools_are_served_after_the_handshake(
        self, channel_server: _Server
    ) -> None:
        channel_server.initialize()
        tools = channel_server.request("tools/list", {})
        assert tools is not None

        names = {tool["name"] for tool in tools["result"]["tools"]}
        assert {"who", "write", "read_messages"} <= names

    def test_startup_record_names_the_version_and_the_mode(
        self, channel_server: _Server
    ) -> None:
        channel_server.initialize()
        channel_server.request("tools/list", {})

        log = channel_server.log.read_text()
        assert "channel_mode=on" in log
        assert "protocolVersion=2025-11-25" in log

    def test_modern_request_after_initialize_is_refused(
        self, channel_server: _Server
    ) -> None:
        channel_server.initialize()

        response = channel_server.request("tools/list", {"_meta": _MODERN_META})

        assert response is not None
        assert "error" in response

    def test_stdin_close_exits_cleanly_and_closes_the_write_stream(
        self, channel_server: _Server
    ) -> None:
        channel_server.initialize()
        channel_server.close_stdin()

        assert channel_server.at_end_of_stream()
        assert channel_server.wait() == 0


class TestWithoutChannelMode:
    """Every other caller keeps the dual-era loop."""

    def test_probe_is_answered(self, plain_server: _Server) -> None:
        probe = plain_server.request("server/discover", {"_meta": _MODERN_META})

        assert probe is not None
        assert "2026-07-28" in probe["result"]["supportedVersions"]

    def test_capability_is_absent(self, plain_server: _Server) -> None:
        result = plain_server.initialize()["result"]

        assert "claude/channel" not in result["capabilities"].get("experimental", {})

    def test_startup_record_says_channel_mode_is_off(
        self, plain_server: _Server
    ) -> None:
        plain_server.initialize()
        plain_server.request("tools/list", {})

        assert "channel_mode=off" in plain_server.log.read_text()
