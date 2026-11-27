"""The post-merge layers tripwire is loosened only by the PR that landed the push."""

from __future__ import annotations

import io
import json

import pytest

from tools.layers_allowance import ALLOW_FLAG, LABEL, allow_flag, main

MERGE = "2b11dc72075cdbc6858381c3424d2b07c3b6a673"
HEAD = "fa6f7b9889384d8360d48cb8f898e5a8c5b580e3"


def _pull(
    *,
    merge_commit_sha: str,
    labels: tuple[str, ...] = (LABEL,),
    merged: bool = True,
) -> dict[str, object]:
    return {
        "merged_at": "2026-11-26T23:05:00Z" if merged else None,
        "merge_commit_sha": merge_commit_sha,
        "labels": [{"name": name} for name in labels],
    }


def _json(*pulls: dict[str, object]) -> str:
    return json.dumps(list(pulls))


def test_the_labelled_pr_that_merged_this_commit_allows_the_widening() -> None:
    assert allow_flag(_json(_pull(merge_commit_sha=MERGE)), MERGE) == ALLOW_FLAG


def test_a_merge_without_the_label_stays_strict() -> None:
    pulls = _json(_pull(merge_commit_sha=MERGE, labels=("Merge Risk: Medium",)))
    assert allow_flag(pulls, MERGE) == ""


def test_a_labelled_pr_whose_merge_commit_is_another_commit_stays_strict() -> None:
    """The API also lists a PR for each of its own commits, such as its head."""
    assert allow_flag(_json(_pull(merge_commit_sha=MERGE)), HEAD) == ""


def test_a_labelled_pr_that_is_not_merged_stays_strict() -> None:
    pulls = _json(_pull(merge_commit_sha=MERGE, merged=False))
    assert allow_flag(pulls, MERGE) == ""


def test_the_label_on_another_pr_does_not_count() -> None:
    pulls = _json(
        _pull(merge_commit_sha=MERGE, labels=()),
        _pull(merge_commit_sha=HEAD),
    )
    assert allow_flag(pulls, MERGE) == ""


def test_no_associated_pr_stays_strict() -> None:
    assert allow_flag("[]", MERGE) == ""


def test_unreadable_pr_data_is_an_error_not_an_allowance() -> None:
    with pytest.raises(json.JSONDecodeError):
        allow_flag("not json", MERGE)


def test_main_prints_the_flag_for_the_workflow_to_append() -> None:
    out = io.StringIO()
    pulls = io.StringIO(_json(_pull(merge_commit_sha=MERGE)))
    main(["layers_allowance.py", MERGE], pulls, out)
    assert out.getvalue() == f"{ALLOW_FLAG}\n"
