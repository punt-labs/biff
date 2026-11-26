"""Where private keys rest: the keyring, an encrypted file, the macOS keychain."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from biff.key_vault import (
    KeyringVault,
    MacKeychainVault,
    PassphraseFileVault,
    VaultError,
)

SECRET = "ab" * 32


class _FakeKeyring:
    """Stands in for the ``keyring`` module's three functions."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.items.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self.items[service, account] = password


def test_keyring_vault_round_trips() -> None:
    vault = KeyringVault(_FakeKeyring())
    vault.put("biff-agent", "claude", SECRET)
    assert vault.get("biff-agent", "claude") == SECRET


def test_keyring_vault_reports_absence_as_none() -> None:
    assert KeyringVault(_FakeKeyring()).get("biff-agent", "nobody") is None


def test_passphrase_vault_round_trips(tmp_path: Path) -> None:
    vault = PassphraseFileVault(tmp_path, lambda _prompt: "correct horse")
    vault.put("biff-owner", "jfreeman", SECRET)
    assert vault.get("biff-owner", "jfreeman") == SECRET


def test_passphrase_vault_file_does_not_hold_the_secret(tmp_path: Path) -> None:
    vault = PassphraseFileVault(tmp_path, lambda _prompt: "correct horse")
    vault.put("biff-owner", "jfreeman", SECRET)
    contents = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert SECRET.encode() not in contents


def test_passphrase_vault_file_is_private(tmp_path: Path) -> None:
    vault = PassphraseFileVault(tmp_path, lambda _prompt: "correct horse")
    vault.put("biff-owner", "jfreeman", SECRET)
    (path,) = tmp_path.iterdir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_passphrase_vault_rejects_wrong_passphrase(tmp_path: Path) -> None:
    PassphraseFileVault(tmp_path, lambda _p: "right").put("s", "a", SECRET)
    with pytest.raises(VaultError, match="passphrase"):
        PassphraseFileVault(tmp_path, lambda _p: "wrong").get("s", "a")


def test_passphrase_vault_prompts_on_every_read(tmp_path: Path) -> None:
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "pw"

    vault = PassphraseFileVault(tmp_path, ask)
    vault.put("s", "a", SECRET)
    prompts.clear()
    vault.get("s", "a")
    vault.get("s", "a")
    assert len(prompts) == 2


def test_passphrase_vault_absence_is_none_and_never_prompts(tmp_path: Path) -> None:
    def ask(prompt: str) -> str:
        raise AssertionError(prompt)

    assert PassphraseFileVault(tmp_path, ask).get("s", "missing") is None


def test_passphrase_vault_refuses_path_traversal(tmp_path: Path) -> None:
    vault = PassphraseFileVault(tmp_path, lambda _p: "pw")
    with pytest.raises(VaultError, match="account"):
        vault.put("s", "../escape", SECRET)


class _Security:
    """Records ``security`` invocations and serves them like the real tool.

    ``-i`` reads one command per stdin line, as the real tool does.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.stored: str | None = None

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append((args, stdin))
        if args == ["-i"] and stdin is not None:
            words = stdin.split()
            self.stored = words[words.index("-w") + 1]
            return ""
        return f"{self.stored}\n"

    @property
    def argv(self) -> list[str]:
        """Every argument ever passed to the subprocess."""
        return [word for args, _ in self.calls for word in args]


def test_mac_vault_secret_is_never_in_argv() -> None:
    run = _Security()
    MacKeychainVault(run).put("biff-owner", "jfreeman", SECRET)
    assert SECRET not in " ".join(run.argv)


def test_mac_vault_secret_travels_on_stdin() -> None:
    run = _Security()
    MacKeychainVault(run).put("biff-owner", "jfreeman", SECRET)
    assert run.calls[0] == (["-i"], run.calls[0][1])
    assert SECRET in (run.calls[0][1] or "")


def test_mac_vault_item_trusts_no_application() -> None:
    run = _Security()
    MacKeychainVault(run).put("biff-owner", "jfreeman", SECRET)
    assert ' -T "" ' in (run.calls[0][1] or "")


def test_mac_vault_stdin_is_one_command_line() -> None:
    run = _Security()
    MacKeychainVault(run).put("biff-owner", "jfreeman", SECRET)
    assert (run.calls[0][1] or "").count("\n") == 1


def test_mac_vault_refuses_a_secret_that_is_not_hex() -> None:
    with pytest.raises(VaultError, match="hex"):
        MacKeychainVault(_Security()).put("biff-owner", "jfreeman", 'x" -T "any')


def test_mac_vault_reads_back_what_it_stored() -> None:
    run = _Security()
    vault = MacKeychainVault(run)
    vault.put("biff-owner", "jfreeman", SECRET)
    assert vault.get("biff-owner", "jfreeman") == SECRET


def test_keyring_vault_has_follows_put() -> None:
    vault = KeyringVault(_FakeKeyring())
    before = vault.has("biff-agent", "claude")
    vault.put("biff-agent", "claude", SECRET)
    assert (before, vault.has("biff-agent", "claude")) == (False, True)


def test_passphrase_vault_has_never_prompts(tmp_path: Path) -> None:
    def ask(prompt: str) -> str:
        raise AssertionError(prompt)

    vault = PassphraseFileVault(tmp_path, ask)
    assert not vault.has("s", "a")


def test_mac_vault_has_does_not_ask_for_the_secret() -> None:
    run = _Security()
    MacKeychainVault(run).has("biff-owner", "jfreeman")
    assert "-w" not in run.calls[0][0]


def test_mac_vault_quotes_a_keychain_path_with_spaces_and_quotes() -> None:
    run = _Security()
    MacKeychainVault(run, Path('/tmp/a b/"x".keychain')).put("s", "a", SECRET)
    assert (run.calls[0][1] or "").endswith(' "/tmp/a b/\\"x\\".keychain"\n')
