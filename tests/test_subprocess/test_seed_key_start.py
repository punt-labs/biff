"""A real biffd makes a seed key only with a seed, and survives a bad key path."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.subprocess

_PATIENCE: Final = 30.0
_KEY: Final = "relay-seed.key"


def _run(home: Path) -> Path:
    return home / ".punt-labs" / "biff" / "run"


def _make_key_directory(home: Path) -> None:
    """Make biffd's run directory, private as biffd demands, with a directory as key."""
    _run(home).mkdir(parents=True)
    _run(home).chmod(0o700)
    (_run(home) / _KEY).mkdir(mode=0o700)


def _log(home: Path) -> str:
    log = home / ".punt-labs" / "biff" / "logs" / "biff.log"
    return log.read_text() if log.exists() else ""


@contextmanager
def _serving(home: Path, extra: dict[str, str]) -> Generator[subprocess.Popen[bytes]]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BIFF_RELAY_")}
    env |= {"HOME": str(home), "PUNT_TELEMETRY": "off", **extra}
    proc = subprocess.Popen(
        [sys.executable, "-m", "biff", "daemon", "serve", "--port", "0"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + _PATIENCE
        while not (_run(home) / "serve.port").exists():
            assert proc.poll() is None, f"biffd exited: {_log(home)}"
            assert time.monotonic() < deadline, "biffd never wrote its port file"
            time.sleep(0.05)
        yield proc
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


def test_a_start_with_no_relay_settings_makes_no_key(tmp_path: Path) -> None:
    with _serving(tmp_path, {}):
        pass
    assert (
        (_run(tmp_path) / _KEY).exists(),
        "key" in _log(tmp_path).casefold(),
    ) == (False, False)


def test_a_directory_at_the_key_path_does_not_stop_a_start_with_no_settings(
    tmp_path: Path,
) -> None:
    _make_key_directory(tmp_path)
    with _serving(tmp_path, {}):
        pass
    assert (_run(tmp_path) / _KEY).is_dir()


def test_a_directory_at_the_key_path_is_replaced_when_there_is_a_seed(
    tmp_path: Path,
) -> None:
    _make_key_directory(tmp_path)
    seeded = {
        "BIFF_RELAY_URL": "nats://127.0.0.1:1",
        "BIFF_RELAY_TOKEN": "tok-seeded-0123456789",
    }
    with _serving(tmp_path, seeded):
        pass
    assert ((_run(tmp_path) / _KEY).is_file(), _KEY in _log(tmp_path)) == (True, True)
