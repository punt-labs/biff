"""Choosing vaults for this machine, and reading a passphrase only from the tty."""

from __future__ import annotations

import logging
from pathlib import Path

import keyring.backends.fail
import pytest
from keyring.errors import KeyringError

from biff import key_wiring
from biff.key_vault import KeyringVault, SecretVault, VaultError, VaultKey
from biff.key_wiring import (
    KeyringProbe,
    SystemKeyring,
    TtyPassphrase,
    machine_vaults,
)
from biff.keychain_vault import MacKeychainVault
from biff.passphrase_vault import PassphraseFileVault


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
    return machine_vaults(
        tmp_path,
        lambda _p: "a long enough passphrase",
        platform=platform,
        backend=probe,
    )


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


def test_the_fallback_to_a_file_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="biff.key_wiring"):
        _vaults(tmp_path, "linux", _Probe(usable=False))
    assert "no keyring backend" in caplog.text


def test_a_probe_that_errors_does_not_fall_back_to_the_file(tmp_path: Path) -> None:
    class Broken(_Probe):
        def is_usable(self) -> bool:
            msg = "keyring probe failed"
            raise VaultError(msg)

    with pytest.raises(VaultError, match="probe failed"):
        _vaults(tmp_path, "linux", Broken(usable=True))


def test_agent_keys_stay_in_the_keyring_they_were_created_in(tmp_path: Path) -> None:
    _vaults(tmp_path, "linux", _Probe(usable=True))
    with pytest.raises(VaultError, match="OS keyring"):
        _vaults(tmp_path, "linux", _Probe(usable=False))


def test_agent_keys_stay_in_files_when_a_keyring_appears_later(tmp_path: Path) -> None:
    _vaults(tmp_path, "linux", _Probe(usable=False))
    _, agent = _vaults(tmp_path, "linux", _Probe(usable=True))
    assert isinstance(agent, PassphraseFileVault)


def test_an_unreadable_backend_record_is_a_vault_error(tmp_path: Path) -> None:
    keys = tmp_path / ".punt-labs" / "biff" / "keys"
    keys.mkdir(parents=True)
    (keys / "agent-backend").write_text("floppy disk")
    with pytest.raises(VaultError, match="agent-backend"):
        _vaults(tmp_path, "linux", _Probe(usable=True))


def test_key_files_live_under_the_home_directory(tmp_path: Path) -> None:
    owner, agent = _vaults(tmp_path / "home", "linux", _Probe(usable=False))
    owner.put(VaultKey("biff-owner", "me"), "ab" * 32)
    agent.put(VaultKey("biff-agent", "claude"), "cd" * 32)
    files = {
        p.relative_to(tmp_path / "home") for p in (tmp_path / "home").rglob("*.enc")
    }
    assert {f.parts[:3] for f in files} == {(".punt-labs", "biff", "keys")}


def test_the_fail_backend_means_no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "get_keyring", keyring.backends.fail.Keyring)
    assert not SystemKeyring().is_usable()


def test_any_other_backend_is_a_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    class Real:
        priority = 5.0

    monkeypatch.setattr(keyring, "get_keyring", Real)
    assert SystemKeyring().is_usable()


def test_a_probe_failure_is_a_vault_error_not_no_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> object:
        msg = "dbus exploded"
        raise KeyringError(msg)

    monkeypatch.setattr(keyring, "get_keyring", boom)
    with pytest.raises(VaultError, match="dbus exploded"):
        SystemKeyring().is_usable()


def test_a_keyring_read_failure_is_a_vault_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_service: str, _account: str) -> str | None:
        msg = "locked"
        raise KeyringError(msg)

    monkeypatch.setattr(keyring, "get_password", boom)
    with pytest.raises(VaultError, match="locked"):
        KeyringVault(SystemKeyring()).get(VaultKey("biff-agent", "claude"))


def test_the_passphrase_is_read_through_the_tty(tmp_path: Path) -> None:
    tty = tmp_path / "tty"
    tty.write_text("")
    ask = TtyPassphrase(tty_path=tty, reader=lambda _prompt: "secret")
    assert ask("p: ") == "secret"


def test_no_tty_is_an_error_and_never_falls_back_to_stdin(tmp_path: Path) -> None:
    def reader(_prompt: str) -> str:
        msg = "read a passphrase without a tty"
        raise AssertionError(msg)

    with pytest.raises(VaultError, match="/dev/tty"):
        TtyPassphrase(tty_path=tmp_path / "missing", reader=reader)("p: ")


def test_the_default_passphrase_reader_is_the_tty_one() -> None:
    assert isinstance(key_wiring.DEFAULT_ASK, TtyPassphrase)
