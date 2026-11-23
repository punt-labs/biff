"""``scripts/smoke-release.sh`` rejects bad arguments before installing anything."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke-release.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(_SCRIPT), *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_help_prints_usage() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "candidate VERSION WHEEL" in result.stdout


@pytest.mark.parametrize(
    "args",
    [(), ("bogus", "1.0.0"), ("published",), ("published", "1.0.0", "extra")],
)
def test_unknown_mode_or_arity_is_a_usage_error(args: tuple[str, ...]) -> None:
    result = _run(*args)
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_candidate_needs_exactly_one_wheel() -> None:
    result = _run("candidate", "1.0.0")
    assert result.returncode == 1
    assert "Pass exactly one candidate wheel" in result.stderr


def test_candidate_rejects_a_missing_wheel(tmp_path: Path) -> None:
    result = _run("candidate", "1.0.0", str(tmp_path / "punt_biff-1.0.0.whl"))
    assert result.returncode == 1
    assert "Expected an existing wheel" in result.stderr


def test_candidate_rejects_a_non_wheel(tmp_path: Path) -> None:
    sdist = tmp_path / "punt_biff-1.0.0.tar.gz"
    sdist.write_bytes(b"")
    result = _run("candidate", "1.0.0", str(sdist))
    assert result.returncode == 1
    assert "Expected an existing wheel" in result.stderr


@pytest.mark.parametrize("version", ["v1.0.0", "", "1.0.0; rm -rf /"])
def test_published_rejects_a_malformed_version(version: str) -> None:
    result = _run("published", version)
    assert result.returncode == 1
    assert "PEP 440 version" in result.stderr
