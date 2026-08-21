"""Tests for the FastMCP application factory."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp import FastMCP

from biff.server import app
from biff.server.app import (
    _UNEXPECTED_CLEANUP_ERROR,
    _lifespan_cleanup,
    _release_session,
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

    def test_failed_stderr_write_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A closed/invalid fd 2 must not stop the loop from returning.

        Reporting a step's failure is itself a write to fd 2 -- if that
        write raised uncaught, the exception would escape
        ``_run_signal_cleanup_steps`` and skip the terminating
        ``os.kill`` that follows it in ``_signal_handler``,
        reintroducing the exact orphan-process hang this handler exists to
        prevent -- on the failure-reporting path itself.
        """

        def _closed_fd_write(_fd: int, _data: bytes) -> int:
            raise OSError("Bad file descriptor")

        monkeypatch.setattr("os.write", _closed_fd_write)

        def _fails() -> None:
            raise OSError("disk full")

        _run_signal_cleanup_steps([(_fails, (OSError,), b"specific label\n")])

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
        def _boom(*args: object) -> Path:
            # ``Path.home`` is a classmethod; monkeypatching it with a plain
            # function can be invoked with the implicit class argument
            # depending on how the attribute is accessed, so this must accept
            # (and ignore) any arguments rather than a fixed zero-arg
            # signature -- a mismatch here raises TypeError instead of the
            # RuntimeError this test means to exercise.
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

        def _boom(*args: object) -> Path:
            # See the sibling test above for why this accepts *args.
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


class TestLifespanCleanupSentinelOrdering:
    """The reap-fallback sentinel must be written after the reaper stops
    ticking, but before any network-bound teardown step that could hang.

    Writing it before the reaper stops races ``_reap_sentinels``, which
    treats any sentinel matching this session's own key as a prior
    incarnation and discards it unreaped -- consuming the
    fallback before a later timed-out ``_release_relay`` ever needs it
    (Cursor Bugbot, High). Writing it only after the logout/task-shutdown
    awaits reopens the same failure from the other side: those awaits sit
    inside FastMCP's cancellable disconnect budget, so a cancellation
    before they complete would again leave no sentinel at all (Cursor
    Bugbot, High, on an earlier version of this fix).
    """

    async def test_sentinel_written_between_reaper_stop_and_logout(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def _stoppable_reaper() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("reaper_cancelled")
                raise

        async def _fake_append_logout(_state: ServerState) -> None:
            calls.append("append_logout")

        async def _fake_append_companion_logout(_state: ServerState) -> None:
            calls.append("append_companion_logout")

        async def _fake_shutdown_tasks(
            _shutdown: asyncio.Event, _tasks: list[asyncio.Task[None]]
        ) -> None:
            calls.append("shutdown_tasks")

        def _fake_write_sentinels(_state: ServerState) -> None:
            calls.append("write_sentinels")

        async def _fake_release_relay(_state: ServerState) -> None:
            calls.append("release_relay")

        async def _fake_drain() -> None:
            return None

        monkeypatch.setattr(app, "_append_logout_event", _fake_append_logout)
        monkeypatch.setattr(
            app, "_append_companion_logout_event", _fake_append_companion_logout
        )
        monkeypatch.setattr(app, "_shutdown_tasks", _fake_shutdown_tasks)
        monkeypatch.setattr(
            app, "_write_reap_fallback_sentinels", _fake_write_sentinels
        )
        monkeypatch.setattr(app, "_release_relay", _fake_release_relay)
        monkeypatch.setattr("biff.integration.vox.drain_background_tasks", _fake_drain)

        reaper = asyncio.create_task(_stoppable_reaper())
        await asyncio.sleep(0)  # let the reaper task actually start running
        await _lifespan_cleanup(state, asyncio.Event(), reaper, [])

        assert calls.index("reaper_cancelled") < calls.index("write_sentinels")
        assert calls.index("write_sentinels") < calls.index("append_logout")
        assert calls.index("append_logout") < calls.index("shutdown_tasks")
        assert calls.index("shutdown_tasks") < calls.index("release_relay")


class TestReleaseSessionSentinelRemovalDoesNotAbort:
    """A failure removing the now-stale sentinel must not skip the rest of
    teardown (companion release, ``relay.close()``) -- the row itself is
    already gone, so re-processing it on the next reaper tick is harmless,
    unlike losing steps still queued after this one (Cursor Bugbot, Medium).
    """

    async def test_oserror_removing_sentinel_does_not_propagate(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_repo_name: str, _session_key: str) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(app, "_remove_sentinel", _boom)

        # delete_session succeeds even for a session key LocalRelay has
        # never seen, so this reaches the sentinel-removal branch without
        # needing a full session registered first.
        await _release_session(
            state, user=state.config.user, session_key=state.session_key, tty_name=None
        )


class TestLifespanCleanupSurvivesFailedReaper:
    """A reaper that failed with its own exception before cancellation
    must not abort the rest of ``_lifespan_cleanup``.

    ``_reap_loop`` has no try/except around ``_reap_sentinels``, so an
    unhandled error there leaves the task done-with-exception rather than
    cancelled; a bare ``await reaper`` re-raises that stored exception,
    skipping the sentinel write and ``_release_relay`` -- the exact
    failure mode ``_shutdown_tasks``'s ``gather(..., return_exceptions=True)``
    already protects the other background tasks against (Cursor Bugbot,
    High).
    """

    async def test_reaper_runtime_error_does_not_abort_cleanup(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def _failing_reaper() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        async def _fake_append_logout(_state: ServerState) -> None:
            calls.append("append_logout")

        async def _fake_append_companion_logout(_state: ServerState) -> None:
            calls.append("append_companion_logout")

        async def _fake_shutdown_tasks(
            _shutdown: asyncio.Event, _tasks: list[asyncio.Task[None]]
        ) -> None:
            calls.append("shutdown_tasks")

        def _fake_write_sentinels(_state: ServerState) -> None:
            calls.append("write_sentinels")

        async def _fake_release_relay(_state: ServerState) -> None:
            calls.append("release_relay")

        async def _fake_drain() -> None:
            return None

        monkeypatch.setattr(app, "_append_logout_event", _fake_append_logout)
        monkeypatch.setattr(
            app, "_append_companion_logout_event", _fake_append_companion_logout
        )
        monkeypatch.setattr(app, "_shutdown_tasks", _fake_shutdown_tasks)
        monkeypatch.setattr(
            app, "_write_reap_fallback_sentinels", _fake_write_sentinels
        )
        monkeypatch.setattr(app, "_release_relay", _fake_release_relay)
        monkeypatch.setattr("biff.integration.vox.drain_background_tasks", _fake_drain)

        reaper = asyncio.create_task(_failing_reaper())
        await asyncio.sleep(0)  # let the reaper task actually run and raise
        await _lifespan_cleanup(state, asyncio.Event(), reaper, [])

        assert calls == [
            "write_sentinels",
            "append_logout",
            "append_companion_logout",
            "shutdown_tasks",
            "release_relay",
        ]
