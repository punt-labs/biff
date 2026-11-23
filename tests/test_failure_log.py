"""``EpisodeLog``: expected failures latch per type, bugs are never masked."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

import anyio
import nats.errors
import nats.js.errors
import pytest

from biff import failure_log
from biff.failure_log import REMINDER_SECONDS, EpisodeLog
from biff.relay_errors import RELAY_ERRORS, RelayLog
from biff.server.channel import ChannelUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

_LOG = logging.getLogger("episode-test")
_WHAT = "Thing"
_EXPECTED = (TimeoutError, ChannelUnavailableError, anyio.ClosedResourceError, OSError)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    now = [1000.0]
    monkeypatch.setattr(failure_log, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def advance(seconds: float) -> None:
        now[0] += seconds

    return advance


def _log() -> EpisodeLog:
    return EpisodeLog(_LOG, _EXPECTED)


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == level]


class TestBugsAreNeverMasked:
    def test_a_bug_after_an_open_episode_is_logged_at_error_with_a_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = _log()
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            log.failed(_WHAT, ChannelUnavailableError("dead"))
            log.failed(_WHAT, TypeError("bug"))

        errors = _records(caplog, logging.ERROR)
        assert len(errors) == 1
        assert errors[0].exc_info is not None
        assert "TypeError" in errors[0].getMessage()

    def test_the_same_bug_type_is_logged_once_and_a_new_type_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = _log()
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            for _ in range(5):
                log.failed(_WHAT, TypeError("bug"))
            log.failed(_WHAT, KeyError("other"))

        assert len(_records(caplog, logging.ERROR)) == 2

    def test_a_recovered_label_logs_its_bug_again_next_time(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = _log()
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            log.failed(_WHAT, TypeError("bug"))
            log.recovered(_WHAT)
            log.failed(_WHAT, TypeError("bug"))

        assert len(_records(caplog, logging.ERROR)) == 2


class TestExpectedFailuresLatchPerType:
    def test_each_expected_type_opens_its_own_episode(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = _log()
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            for _ in range(3):
                log.failed(_WHAT, TimeoutError())
            log.failed(_WHAT, OSError("pipe"))

        assert len(_records(caplog, logging.WARNING)) == 2

    def test_a_still_failing_reminder_names_duration_attempts_and_cause(
        self, caplog: pytest.LogCaptureFixture, clock: Callable[[float], None]
    ) -> None:
        log = _log()
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            log.failed(_WHAT, TimeoutError())
            clock(30)
            log.failed(_WHAT, TimeoutError())
            clock(REMINDER_SECONDS)
            log.failed(_WHAT, TimeoutError())

        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 2
        assert "still failing" in warnings[1].getMessage()
        assert "3 attempts" in warnings[1].getMessage()

    def test_recovery_is_a_warning_so_it_reaches_stderr(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = _log()
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            log.failed(_WHAT, TimeoutError())
            log.recovered(_WHAT)
            log.recovered(_WHAT)

        recoveries = [r for r in caplog.records if "recovered" in r.getMessage()]
        assert [r.levelno for r in recoveries] == [logging.WARNING]


class TestThroughTheCallers:
    async def test_a_poller_bug_after_a_timeout_is_not_masked(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from biff.server.tools import _descriptions

        _descriptions._reset_session()
        calls = [TimeoutError("slow"), KeyError("bug"), KeyError("bug")]

        async def tick(*_args: object, **_kwargs: object) -> None:
            raise calls.pop(0)

        monkeypatch.setattr(_descriptions, "_active_tick", tick)
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await _descriptions._safe_tick(
                    None,  # type: ignore[arg-type]
                    None,  # type: ignore[arg-type]
                    0,
                    ("", ""),
                    ((), 0, ""),
                    gate=None,  # type: ignore[arg-type]
                )

        assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.ERROR]


class TestALastingBugReminds:
    def test_a_bug_that_never_goes_away_logs_error_every_reminder_period(
        self, caplog: pytest.LogCaptureFixture, clock: Callable[[float], None]
    ) -> None:
        log = _log()
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            for _ in range(200):
                clock(30)
                log.failed(_WHAT, TypeError("bug"))

        errors = _records(caplog, logging.ERROR)
        assert len(errors) == 1 + (6000 - 30) // int(REMINDER_SECONDS)
        assert "still failing" in errors[-1].getMessage()
        assert "attempts" in errors[-1].getMessage()


class TestThePollerTreatsANatsOutageAsTransport:
    async def test_a_connection_closed_tick_is_a_warning_with_reminders(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        clock: Callable[[float], None],
    ) -> None:
        import nats.errors

        from biff.server.tools import _descriptions

        _descriptions._reset_session()

        async def tick(*_args: object, **_kwargs: object) -> None:
            raise nats.errors.ConnectionClosedError

        monkeypatch.setattr(_descriptions, "_active_tick", tick)
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                await _descriptions._safe_tick(
                    None,  # type: ignore[arg-type]
                    None,  # type: ignore[arg-type]
                    0,
                    ("", ""),
                    ((), 0, ""),
                    gate=None,  # type: ignore[arg-type]
                )
            clock(REMINDER_SECONDS)
            await _descriptions._safe_tick(
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                0,
                ("", ""),
                ((), 0, ""),
                gate=None,  # type: ignore[arg-type]
            )

        assert [r.levelno for r in caplog.records] == [logging.WARNING] * 2


class TestRelayErrors:
    """``RELAY_ERRORS`` names what an unreachable relay raises."""

    @pytest.mark.parametrize(
        "error",
        [
            nats.errors.ConnectionClosedError(),
            nats.errors.NoServersError(),
            nats.errors.StaleConnectionError(),
            nats.js.errors.ServiceUnavailableError(),
            TimeoutError(),
            OSError(),
        ],
    )
    def test_an_outage_is_expected(self, error: Exception) -> None:
        assert isinstance(error, RELAY_ERRORS)

    @pytest.mark.parametrize("error", [KeyError("x"), TypeError("x"), RuntimeError()])
    def test_a_code_bug_is_not(self, error: Exception) -> None:
        assert not isinstance(error, RELAY_ERRORS)

    def test_the_relay_log_warns_for_an_outage_and_errors_for_a_bug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = RelayLog(_LOG)
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            log.failed(_WHAT, nats.errors.NoServersError())
            log.failed(_WHAT, KeyError("bug"))

        assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.ERROR]
