"""Decide whether the post-merge layers tripwire may allow a policy widening.

A widening of a layer's imports lands through a pull request a reviewer labelled
``layers-policy-change``, and the pull-request job accepts it for that label.
The tripwire on ``main`` has to accept the same change once it lands, but only on
the word of the pull request that produced the pushed commit: the API also lists
a pull request for each of its own commits, and for open pull requests that
contain the commit, and a label on one of those must not loosen the gate.

Reads the JSON the commit's pulls endpoint returns on stdin and prints the flag
to append, or an empty line.  Only that fixed flag leaves this script, so a label
name never reaches the shell as text.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

__all__ = ["ALLOW_FLAG", "LABEL", "allow_flag", "main"]

LABEL = "layers-policy-change"
ALLOW_FLAG = "--allow-config-change"


def _labelled_merge(pull: object, sha: str) -> bool:
    """Return whether *pull* is merged, was merged as *sha*, and carries the label."""
    if not isinstance(pull, dict):
        return False
    fields = cast("dict[str, object]", pull)
    if fields.get("merged_at") is None or fields.get("merge_commit_sha") != sha:
        return False
    labels = fields.get("labels")
    if not isinstance(labels, list):
        return False
    names = {
        cast("dict[str, object]", label).get("name")
        for label in cast("list[object]", labels)
        if isinstance(label, dict)
    }
    return LABEL in names


def allow_flag(pulls_json: str, sha: str) -> str:
    """Return the flag that allows a layers widening for the push of *sha*.

    The flag is returned only when a merged pull request whose merge commit is
    *sha* carries the label; otherwise the result is empty and the tripwire
    stays strict.

    Raises:
        json.JSONDecodeError: *pulls_json* is not JSON.
    """
    pulls = json.loads(pulls_json)
    if not isinstance(pulls, list):
        return ""
    if any(_labelled_merge(pull, sha) for pull in cast("list[object]", pulls)):
        return ALLOW_FLAG
    return ""


def main(argv: Sequence[str], stdin: TextIO, stdout: TextIO) -> None:
    """Print the flag for the sha in ``argv[1]``, from the pulls JSON on *stdin*."""
    stdout.write(f"{allow_flag(stdin.read(), argv[1])}\n")


if __name__ == "__main__":
    main(sys.argv, sys.stdin, sys.stdout)
