"""Choosing vaults for this machine."""

from __future__ import annotations

from pathlib import Path

from biff.key_vault import (
    KeyringVault,
    MacKeychainVault,
    PassphraseFileVault,
    SecretVault,
)
from biff.key_wiring import KeyringProbe, machine_vaults


class _Probe:
    def __init__(self, *, usable: bool) -> None:
        self._usable = usable

    def is_usable(self) -> bool:
        return self._usable

    def get_password(self, service: str, account: str) -> str | None:
        return None

    def set_password(self, service: str, account: str, password: str) -> None:
        return None


def _vaults(
    tmp_path: Path, platform: str, probe: KeyringProbe
) -> tuple[SecretVault, SecretVault]:
    return machine_vaults(tmp_path, lambda _p: "pw", platform=platform, backend=probe)


def test_macos_keeps_the_owner_key_in_the_keychain_with_no_trusted_app(
    tmp_path: Path,
) -> None:
    owner, _ = _vaults(tmp_path, "darwin", _Probe(usable=True))
    assert isinstance(owner, MacKeychainVault)


def test_linux_keeps_the_owner_key_behind_a_passphrase(tmp_path: Path) -> None:
    owner, _ = _vaults(tmp_path, "linux", _Probe(usable=True))
    assert isinstance(owner, PassphraseFileVault)


def test_agent_keys_use_the_keyring_when_one_works(tmp_path: Path) -> None:
    _, agent = _vaults(tmp_path, "linux", _Probe(usable=True))
    assert isinstance(agent, KeyringVault)


def test_headless_linux_falls_back_to_an_encrypted_file(tmp_path: Path) -> None:
    _, agent = _vaults(tmp_path, "linux", _Probe(usable=False))
    assert isinstance(agent, PassphraseFileVault)


def test_key_files_live_under_the_home_directory(tmp_path: Path) -> None:
    owner, agent = _vaults(tmp_path / "home", "linux", _Probe(usable=False))
    owner.put("biff-owner", "me", "ab" * 32)
    agent.put("biff-agent", "claude", "cd" * 32)
    files = {
        p.relative_to(tmp_path / "home") for p in (tmp_path / "home").rglob("*.enc")
    }
    assert {f.parts[:3] for f in files} == {(".punt-labs", "biff", "keys")}
