"""On macOS an owner key cannot be read unless the person answers a dialog.

Manual proof, never part of a default run: it drives the real ``security``
tool against a throwaway keychain, and reading the owner item raises a macOS
dialog.  Run it by hand, at the screen, with ``pytest -m keychain``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from biff.keychain_vault import MacKeychainVault, run_security

pytestmark = pytest.mark.keychain

SECRET = "ab" * 32


@pytest.fixture
def keychain(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "test.keychain-db"
    run_security(["create-keychain", "-p", "testpw", str(path)])
    yield path
    run_security(["delete-keychain", str(path)])


def test_owner_item_is_not_released_without_the_person(keychain: Path) -> None:
    vault = MacKeychainVault(keychain=keychain)
    vault.put("biff-owner", "probe", SECRET)
    read = ["find-generic-password", "-s", "biff-owner", "-a", "probe", "-w"]
    with pytest.raises((subprocess.TimeoutExpired, subprocess.CalledProcessError)):
        run_security([*read, str(keychain)], timeout=8)


def test_item_without_the_empty_access_list_reads_without_a_dialog(
    keychain: Path,
) -> None:
    add = ["add-generic-password", "-s", "biff-control", "-a", "probe", "-w", "cafe"]
    run_security([*add, str(keychain)])
    read = ["find-generic-password", "-s", "biff-control", "-a", "probe", "-w"]
    out = run_security([*read, str(keychain)], timeout=8)
    assert out.strip() == "cafe"
