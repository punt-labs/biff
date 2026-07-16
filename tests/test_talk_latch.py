"""Tests for the talk-notification onset/recovery logging latch."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from biff.talk_latch import TalkNotifyLatch

if TYPE_CHECKING:
    import pytest

_LOGGER_NAME = "biff.talk_latch"


def _fail(latch: TalkNotifyLatch) -> None:
    """Record a failure from inside an active ``except`` so ``exc_info`` is real."""
    try:
        raise TimeoutError("nats wedged")
    except TimeoutError:
        latch.record_failure()


class TestTalkNotifyLatch:
    """One WARNING on onset, DEBUG per retry, one INFO on recovery."""

    def test_consecutive_failures_warn_exactly_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(_LOGGER_NAME))
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
        latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(_LOGGER_NAME))
        _fail(latch)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            latch.record_success()
            latch.record_success()  # already recovered — silent

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1

    def test_steady_success_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            for _ in range(5):
                latch.record_success()

        assert caplog.records == []

    def test_recovery_relatches_on_next_outage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            _fail(latch)  # WARNING 1
            latch.record_success()  # INFO 1
            _fail(latch)  # WARNING 2 — re-armed

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(warnings) == 2
        assert len(infos) == 1


class TestLatchWordingByFlavour:
    """Each flavour names its own cause — a fetch failure is not a re-subscribe."""

    def test_fetch_onset_names_the_inbox_read_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A persistent fetch failure warns once and names the inbox/relay cause.

        Routing the durable-inbox fetch through the re-subscribe wording would
        misattribute an inbox-read failure as a subscription failure; the SUB
        can be healthy while the inbox is unreadable.
        """
        latch = TalkNotifyLatch.for_fetch(logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            for _ in range(3):
                _fail(latch)  # persistent fetch failure

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1  # onset only, not one per tick
        message = warnings[0].getMessage().lower()
        assert "inbox" in message
        assert "re-subscribe" not in message

    def test_fetch_recovery_names_the_inbox(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        latch = TalkNotifyLatch.for_fetch(logging.getLogger(_LOGGER_NAME))
        _fail(latch)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            latch.record_success()

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1
        message = infos[0].getMessage().lower()
        assert "inbox" in message
        assert "re-subscribe" not in message

    def test_resubscribe_onset_names_the_resubscribe_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            _fail(latch)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "re-subscribe" in warnings[0].getMessage().lower()
