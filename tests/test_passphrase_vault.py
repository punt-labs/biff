"""A passphrase-sealed key file: confirmed, strong, atomic and self-describing."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from biff.key_vault import VaultError
from biff.passphrase_vault import PassphraseFileVault

SECRET = "ab" * 32
PASS = "correct horse battery"


def _vault(tmp_path: Path, passphrase: str = PASS) -> PassphraseFileVault:
    return PassphraseFileVault(tmp_path, lambda _prompt: passphrase)


def test_passphrase_vault_round_trips(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.put("biff-owner", "jfreeman", SECRET)
    assert vault.get("biff-owner", "jfreeman") == SECRET


def test_passphrase_vault_file_does_not_hold_the_secret(tmp_path: Path) -> None:
    _vault(tmp_path).put("biff-owner", "jfreeman", SECRET)
    contents = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert SECRET.encode() not in contents


def test_passphrase_vault_file_is_private(tmp_path: Path) -> None:
    _vault(tmp_path).put("biff-owner", "jfreeman", SECRET)
    (path,) = tmp_path.iterdir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_passphrase_vault_stores_its_scrypt_parameters(tmp_path: Path) -> None:
    _vault(tmp_path).put("s", "a", SECRET)
    blob = json.loads((tmp_path / "s.a.enc").read_text())
    assert (blob["v"], blob["n"], blob["r"], blob["p"]) == (1, 2**17, 8, 1)


def test_passphrase_vault_reads_back_the_parameters_it_stored(tmp_path: Path) -> None:
    _vault(tmp_path).put("s", "a", SECRET)
    path = tmp_path / "s.a.enc"
    blob = json.loads(path.read_text())
    blob["n"] = 2**30  # a different cost than the key was sealed under
    path.write_text(json.dumps(blob))
    with pytest.raises(VaultError, match="scrypt"):
        _vault(tmp_path).get("s", "a")


def test_passphrase_vault_rejects_wrong_passphrase(tmp_path: Path) -> None:
    _vault(tmp_path, "right right right").put("s", "a", SECRET)
    with pytest.raises(VaultError, match="passphrase"):
        _vault(tmp_path, "wrong wrong wrong").get("s", "a")


def test_passphrase_vault_prompts_on_every_read(tmp_path: Path) -> None:
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return PASS

    vault = PassphraseFileVault(tmp_path, ask)
    vault.put("s", "a", SECRET)
    prompts.clear()
    vault.get("s", "a")
    vault.get("s", "a")
    assert len(prompts) == 2


def test_passphrase_vault_asks_twice_when_it_creates_the_file(tmp_path: Path) -> None:
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return PASS

    PassphraseFileVault(tmp_path, ask).put("s", "a", SECRET)
    assert len(prompts) == 2


def test_passphrase_vault_refuses_a_confirmation_that_differs(tmp_path: Path) -> None:
    answers = iter([PASS, PASS + "x"])
    vault = PassphraseFileVault(tmp_path, lambda _p: next(answers))
    with pytest.raises(VaultError, match="do not match"):
        vault.put("s", "a", SECRET)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("weak", ["", "short", "elevenchars"])
def test_passphrase_vault_refuses_empty_and_short_passphrases(
    tmp_path: Path, weak: str
) -> None:
    with pytest.raises(VaultError, match="at least 12"):
        _vault(tmp_path, weak).put("s", "a", SECRET)
    assert list(tmp_path.iterdir()) == []


def test_passphrase_vault_absence_is_none_and_never_prompts(tmp_path: Path) -> None:
    def ask(prompt: str) -> str:
        raise AssertionError(prompt)

    assert PassphraseFileVault(tmp_path, ask).get("s", "missing") is None


def test_passphrase_vault_has_never_prompts(tmp_path: Path) -> None:
    def ask(prompt: str) -> str:
        raise AssertionError(prompt)

    assert not PassphraseFileVault(tmp_path, ask).has("s", "a")


def test_passphrase_vault_refuses_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(VaultError, match="account"):
        _vault(tmp_path).put("s", "../escape", SECRET)


def test_a_failed_write_leaves_the_live_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    vault.put("s", "a", SECRET)
    before = (tmp_path / "s.a.enc").read_bytes()

    def broken(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(VaultError, match="s/a"):
        vault.put("s", "a", "cd" * 32)
    assert (tmp_path / "s.a.enc").read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["s.a.enc"]


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"v": 1}', '{"v": 2, "n": 1}', "\x00\xff"]
)
def test_a_corrupt_file_is_a_vault_error_naming_the_item(
    tmp_path: Path, content: str
) -> None:
    (tmp_path / "s.a.enc").write_text(content, encoding="latin-1")
    with pytest.raises(VaultError, match=r"s/a"):
        _vault(tmp_path).get("s", "a")


def test_end_of_input_at_the_prompt_is_a_vault_error(tmp_path: Path) -> None:
    _vault(tmp_path).put("s", "a", SECRET)

    def ask(_prompt: str) -> str:
        raise EOFError

    with pytest.raises(VaultError, match=r"passphrase for s/a"):
        PassphraseFileVault(tmp_path, ask).get("s", "a")


def test_passphrase_vault_names_itself(tmp_path: Path) -> None:
    assert _vault(tmp_path).label == f"an encrypted file in {tmp_path}"
