"""Tests for scripts/install-handoff-runtime.sh."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install-handoff-runtime.sh"


def _current_platform_key() -> str:
    os_name = platform.system()
    arch = platform.machine()
    if os_name == "Linux" and arch == "x86_64":
        return "linux-amd64"
    if os_name == "Darwin" and arch == "arm64":
        return "darwin-arm64"
    pytest.skip(f"unsupported test host platform: {os_name}/{arch}")


def _write_fake_repo(tmp_path: Path, *, platform_key: str, sha256: str) -> Path:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    sidecar_dir = repo / ".punt-labs" / "ethos" / "checks" / "snapshots"
    scripts_dir.mkdir(parents=True)
    sidecar_dir.mkdir(parents=True)

    installer_copy = scripts_dir / "install-handoff-runtime.sh"
    shutil.copy2(INSTALLER, installer_copy)

    sidecar = {
        "checks": {
            "full-gate": {
                "runtime": {
                    "platforms": {
                        platform_key: {
                            "url": (
                                "https://github.com/punt-labs/biff/releases/download/"
                                f"handoff-runtime-v1/biff-handoff-{platform_key}.tar.gz"
                            ),
                            "sha256": sha256,
                        }
                    }
                }
            }
        }
    }
    (sidecar_dir / "biff.yaml").write_text(json.dumps(sidecar), encoding="utf-8")
    return installer_copy


def _make_stub_curl(bin_dir: Path, payload: bytes | None) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    if payload is None:
        script = (
            "#!/usr/bin/env bash\n"
            "echo 'curl should not run on cache hit' >&2\n"
            "exit 99\n"
        )
    else:
        payload_file = bin_dir / "payload.bin"
        payload_file.write_bytes(payload)
        script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "out=''\n"
            "while [[ $# -gt 0 ]]; do\n"
            '  if [[ $1 == "-o" || $1 == "--output" ]]; then\n'
            "    out=$2\n"
            "    shift 2\n"
            "  else\n"
            "    shift\n"
            "  fi\n"
            "done\n"
            f'cp "{payload_file}" "$out"\n'
        )
    curl.write_text(script, encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)


def _run_installer(
    installer: Path, *, home: Path, stub_bin: Path
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
    }
    return subprocess.run(  # noqa: S603
        [str(installer)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_installer_reuses_verified_cached_archive(tmp_path: Path) -> None:
    """An existing archive with the pinned SHA-256 is reused without curl."""
    platform_key = _current_platform_key()
    payload = b"valid-runtime-archive"
    sha = hashlib.sha256(payload).hexdigest()
    installer = _write_fake_repo(tmp_path, platform_key=platform_key, sha256=sha)

    home = tmp_path / "home"
    archive = home / ".punt-labs" / "ethos" / "runtimes" / f"{sha}.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(payload)

    stub_bin = tmp_path / "bin"
    _make_stub_curl(stub_bin, payload=None)

    res = _run_installer(installer, home=home, stub_bin=stub_bin)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(archive)


def test_installer_rejects_checksum_mismatch(tmp_path: Path) -> None:
    """A downloaded archive whose digest disagrees with the pin fails cleanly."""
    platform_key = _current_platform_key()
    expected_sha = hashlib.sha256(b"expected-runtime-archive").hexdigest()
    installer = _write_fake_repo(
        tmp_path, platform_key=platform_key, sha256=expected_sha
    )

    home = tmp_path / "home"
    home.mkdir()
    stub_bin = tmp_path / "bin"
    _make_stub_curl(stub_bin, payload=b"tampered-bytes")

    res = _run_installer(installer, home=home, stub_bin=stub_bin)
    assert res.returncode != 0
    assert "checksum mismatch" in res.stderr
    archive = home / ".punt-labs" / "ethos" / "runtimes" / f"{expected_sha}.tar.gz"
    assert not archive.exists()
