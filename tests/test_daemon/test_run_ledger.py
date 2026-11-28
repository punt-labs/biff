"""The run ledger: a replaced run never comes back, and only recent ones are kept."""

from __future__ import annotations

import pytest

from biff.daemon.refusal import RefusalError
from biff.daemon.run_ledger import _MAX_RETIRED_RUNS, RunLedger
from biff.frame_reply import ErrorCode


def test_a_newer_run_replaces_the_current_one_and_names_it() -> None:
    ledger = RunLedger("a1")
    assert ledger.admit("s", "a2") == "a1"
    assert ledger.current == "a2"


def test_the_current_run_replaces_nothing() -> None:
    ledger = RunLedger("a1")
    assert ledger.admit("s", "a1") == ""


def test_a_client_that_claims_no_run_neither_replaces_nor_is_replaced() -> None:
    ledger = RunLedger("a1")
    assert ledger.admit("s", "") == ""
    assert ledger.current == "a1"


def test_a_first_run_replaces_no_one() -> None:
    ledger = RunLedger("")
    assert ledger.admit("s", "a1") == ""
    assert ledger.current == "a1"


def test_a_replaced_run_is_refused_for_good() -> None:
    ledger = RunLedger("a1")
    ledger.admit("s", "a2")
    with pytest.raises(RefusalError) as caught:
        ledger.admit("s", "a1")
    assert caught.value.reply_error.code is ErrorCode.SESSION_REPLACED
    assert ledger.current == "a2"


def test_only_the_most_recent_replaced_runs_are_remembered() -> None:
    ledger = RunLedger("r0")
    for n in range(1, _MAX_RETIRED_RUNS + 2):
        ledger.admit("s", f"r{n}")
    assert ledger.admit("s", "r0") == f"r{_MAX_RETIRED_RUNS + 1}"  # forgotten: admitted
    with pytest.raises(RefusalError):
        ledger.admit("s", f"r{_MAX_RETIRED_RUNS + 1}")  # still remembered
