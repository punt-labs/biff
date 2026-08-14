"""Tests for the FastMCP application factory."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import FastMCP

from biff.server.app import (
    _UNEXPECTED_CLEANUP_ERROR,
    _run_signal_cleanup_steps,
    _write_sentinel,
    create_server,
)
from biff.server.state import ServerState


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Patch ``os.write`` and capture each write's bytes payload."""
    captured: list[bytes] = []

    def _fake_write(_fd: int, data: bytes) -> int:
        captured.append(data)
        return len(data)

    monkeypatch.setattr("os.write", _fake_write)
    return captured


class TestCreateServer:
    def test_returns_fastmcp_instance(self, state: ServerState) -> None:
        mcp = create_server(state)
        assert isinstance(mcp, FastMCP)

    def test_server_name(self, state: ServerState) -> None:
        mcp = create_server(state)
        assert mcp.name == "biff"

    async def test_registers_all_tools(self, state: ServerState) -> None:
        mcp = create_server(state)
        tool_names = {t.name for t in await mcp.list_tools()}
        assert "mesg" in tool_names
        assert "write" in tool_names
        assert "read_messages" in tool_names
        assert "finger" in tool_names
        assert "who" in tool_names
        assert "plan" in tool_names

    async def test_no_duplicate_tools(self, state: ServerState) -> None:
        mcp = create_server(state)
        tools = await mcp.list_tools()
        names = [t.name for t in tools]
        assert len(names) == len(set(names))


class TestSignalCleanupStepsNeverPropagate:
    """No exception from a cleanup step may reach ``_signal_handler``.

    Direct unit test of the runner, not the real-subprocess harness in
    ``tests/test_subprocess/test_signal_handling.py``: the steps it runs
    are built from live closures inside ``_signal_handler`` (state.relay,
    session keys), so injecting a step that raises outside its declared
    tuple into a *running subprocess's* actual signal handler would
    require test-only hooks in production code. Testing the runner
    directly proves the invariant precisely -- no exception escapes the
    loop, whatever type a step raises -- without that scaffolding, and
    the existing subprocess suite already proves termination end to end
    for the declared-exception path.
    """

    def test_declared_exception_reports_its_own_label(
        self, written: list[bytes]
    ) -> None:
        def _fails() -> None:
            raise OSError("disk full")

        _run_signal_cleanup_steps([(_fails, (OSError,), b"specific label\n")])
        assert written == [b"specific label\n"]

    def test_exception_outside_declared_tuple_does_not_propagate(
        self, written: list[bytes]
    ) -> None:
        """The actual guarantee under test: a step's declared tuple can be
        wrong (today or after a future edit) and the loop still cannot
        lose the terminating ``os.kill`` that follows it in
        ``_signal_handler``."""

        def _fails_unexpectedly() -> None:
            raise RuntimeError("not in the declared tuple")

        _run_signal_cleanup_steps(
            [(_fails_unexpectedly, (OSError,), b"specific label\n")]
        )
        assert written == [_UNEXPECTED_CLEANUP_ERROR]

    def test_keyboard_interrupt_does_not_propagate(self, written: list[bytes]) -> None:
        """KeyboardInterrupt (a BaseException, not Exception) must also be
        swallowed here -- a second signal arriving mid-cleanup is exactly
        the case where the exit path must not be lost."""

        def _interrupted() -> None:
            raise KeyboardInterrupt

        _run_signal_cleanup_steps([(_interrupted, (OSError,), b"specific label\n")])
        assert written == [_UNEXPECTED_CLEANUP_ERROR]

    def test_later_steps_still_run_after_an_unexpected_failure(
        self, written: list[bytes]
    ) -> None:
        ran: list[str] = []

        def _fails() -> None:
            raise RuntimeError("unexpected")

        def _succeeds() -> None:
            ran.append("second")

        _run_signal_cleanup_steps(
            [
                (_fails, (OSError,), b"first label\n"),
                (_succeeds, (OSError,), b"second label\n"),
            ]
        )
        assert ran == ["second"]
        assert written == [_UNEXPECTED_CLEANUP_ERROR]


class TestWriteSentinelRuntimeError:
    """_write_sentinel can raise RuntimeError, not just OSError.

    ``sentinel_dir()`` -> ``biff_data_dir()`` calls ``Path.home()`` fresh
    on every ``_write_sentinel`` call, and ``Path.home()`` raises
    ``RuntimeError`` (not ``OSError``) when ``HOME`` is unset and
    ``pwd.getpwuid()`` can't resolve the user.  Not hypothetical: this is
    the concrete instance the ``_run_signal_cleanup_steps`` catch-all
    guards against.
    """

    def test_raises_runtime_error_when_home_unresolvable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Path:
            msg = "could not determine home directory"
            raise RuntimeError(msg)

        monkeypatch.setattr(Path, "home", _boom)
        with pytest.raises(RuntimeError):
            _write_sentinel("_test-repo", "kai:tty1")

    def test_declared_tuple_reports_its_own_label_not_the_catch_all(
        self, written: list[bytes], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduces the exact step ``_signal_handler`` builds for the
        sentinel write -- ``(OSError, RuntimeError)`` -- proving that
        tuple, not the catch-all, is what reports this failure."""

        def _boom() -> Path:
            msg = "could not determine home directory"
            raise RuntimeError(msg)

        monkeypatch.setattr(Path, "home", _boom)
        _run_signal_cleanup_steps(
            [
                (
                    lambda: _write_sentinel("_test-repo", "kai:tty1"),
                    (OSError, RuntimeError),
                    b"biff: signal cleanup: sentinel write failed\n",
                )
            ]
        )
        assert written == [b"biff: signal cleanup: sentinel write failed\n"]
