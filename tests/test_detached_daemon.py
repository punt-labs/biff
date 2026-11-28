"""A detached biffd inherits the starter's relay settings; none reach its argv."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

import pytest

from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.detached import DetachedDaemon

if TYPE_CHECKING:
    from pathlib import Path

_TOKEN: Final = "tok-detached-0123456789"
_PATIENCE_SECONDS: Final = 10.0


def _wait_for(path: Path) -> str:
    deadline = time.monotonic() + _PATIENCE_SECONDS
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().endswith("\n"):
            return path.read_text()
        time.sleep(0.02)
    pytest.fail(f"{path} never appeared")


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An executable that records its argument and one environment variable."""
    script = tmp_path / "probe"
    script.write_text(
        '#!/bin/sh\necho "$@" > "$0.argv.tmp"\nmv "$0.argv.tmp" "$0.argv"\n'
        'echo "$BIFF_RELAY_TOKEN" > "$0.env.tmp"\nmv "$0.env.tmp" "$0.env"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("BIFF_RELAY_TOKEN", _TOKEN)
    return script


def test_the_detached_process_inherits_the_relay_environment(probe: Path) -> None:
    DetachedDaemon(BIFFD_SPEC).launch(probe, 4242)
    assert _wait_for(probe.parent / "probe.env").strip() == _TOKEN


def test_the_detached_command_line_carries_only_the_port(probe: Path) -> None:
    DetachedDaemon(BIFFD_SPEC).launch(probe, 4242)
    assert _wait_for(probe.parent / "probe.argv").strip() == "--port 4242"
