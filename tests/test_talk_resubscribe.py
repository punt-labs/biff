"""Tests for the talk re-subscribe onset/recovery logging latch."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from biff.talk_resubscribe import TalkResubscribeLatch

if TYPE_CHECKING:
    import pytest

_LOGGER_NAME = "biff.talk_resubscribe"


def _fail(latch: TalkResubscribeLatch) -> None:
    """Record a failure from inside an active ``except`` so ``exc_info`` is real."""
    try:
        raise TimeoutError("nats wedged")
    except TimeoutError:
        latch.record_failure()


class TestTalkResubscribeLatch:
    """One WARNING on onset, DEBUG per retry, one INFO on recovery."""

    def test_consecutive_failures_warn_exactly_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        latch = TalkResubscribeLatch(logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            for _ in range(5):
                _fail(latch)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(warnings) == 1  # onset only, not one per tick
        assert len(debugs) == 4  # every retry after onset

    def test_first_success_after_failure_logs_one_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        latch = TalkResubscribeLatch(logging.getLogger(_LOGGER_NAME))
        _fail(latch)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            latch.record_success()
            latch.record_success()  # already recovered — silent

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1

    def test_steady_success_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        latch = TalkResubscribeLatch(logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            for _ in range(5):
                latch.record_success()

        assert caplog.records == []

    def test_recovery_relatches_on_next_outage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        latch = TalkResubscribeLatch(logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            _fail(latch)  # WARNING 1
            latch.record_success()  # INFO 1
            _fail(latch)  # WARNING 2 — re-armed

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(warnings) == 2
        assert len(infos) == 1
