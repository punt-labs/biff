"""biff resolves identity without ever running the ethos CLI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "biff"
PLUGIN = Path(__file__).resolve().parent.parent / "plugin"

# Names of the retired ethos-CLI identity path and the process-tree walk.
RETIRED = (
    "get_ethos_roster",
    "get_ethos_team",
    "EthosSessionPin",
    "SessionPinMiddleware",
    "ethos_session",
)


@pytest.mark.parametrize("name", RETIRED)
def test_retired_name_is_gone_from_src_and_plugin(name: str) -> None:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    hits = [
        f"{path.relative_to(SRC.parent.parent)}:{n}"
        for root in (SRC, PLUGIN)
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh", ".json", ".md"}
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, f"{name} still referenced: {hits}"


def test_no_ethos_subprocess_is_spawned_by_source() -> None:
    pattern = re.compile(r"\[\s*[\"']ethos[\"']")
    hits = [
        str(path.relative_to(SRC.parent.parent))
        for path in SRC.rglob("*.py")
        if pattern.search(path.read_text())
    ]
    assert not hits, f"ethos CLI invoked from: {hits}"
