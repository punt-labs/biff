"""``biff key`` through the real command line entry point."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import biff.__main__ as cli
from biff import cli_key, key_store
from biff.key_vault import SecretVault, VaultError
from biff.nostr_bech32 import Bech32Key
from biff.nostr_crypto import NostrCrypto
from biff.nostr_directory import NostrDirectory
from biff.passphrase_vault import PassphraseFileVault
from tests._memory_vault import MemoryVault

runner = CliRunner()


@pytest.fixture(autouse=True)
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run in a throwaway HOME, as a person at a shell, with a fixed passphrase."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for marker in ("CLAUDECODE", "OPENCODE", "BIFF_ATTESTATION_HOURS"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setattr(key_store, "AGENT_IDENTITY_MOUNT", tmp_path / "no-mount")
    owner = PassphraseFileVault(
        home / "owner-keys", lambda _p: "a long enough passphrase"
    )
    agents: SecretVault = MemoryVault()

    def fake_vaults(_home: Path) -> tuple[SecretVault, SecretVault]:
        return owner, agents

    monkeypatch.setattr(cli_key, "machine_vaults", fake_vaults)
    monkeypatch.setenv("PATH", str(tmp_path / "no-ethos"))


def test_key_init_prints_the_npub() -> None:
    result = runner.invoke(cli.app, ["key", "init", "--owner", "jfreeman"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("your npub: npub1")


def test_key_init_json_carries_the_npub() -> None:
    result = runner.invoke(cli.app, ["--json", "key", "init", "--owner", "jfreeman"])
    assert json.loads(result.output)["npub"].startswith("npub1")


def test_key_init_twice_republishes_and_exits_zero() -> None:
    runner.invoke(cli.app, ["key", "init", "--owner", "jfreeman"])
    result = runner.invoke(cli.app, ["key", "init", "--owner", "jfreeman"])
    assert result.exit_code == 0, result.output
    assert "republished" in result.output


def test_key_init_output_warns_against_always_allow() -> None:
    result = runner.invoke(cli.app, ["key", "init", "--owner", "jfreeman"])
    assert "never click Always Allow" in result.output


def test_key_init_from_an_agent_shell_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    result = runner.invoke(cli.app, ["key", "init", "--owner", "jfreeman"])
    assert result.exit_code == 1
    assert "agent process" in result.output


def test_key_attest_then_show_reports_the_owner() -> None:
    runner.invoke(cli.app, ["key", "init", "--owner", "jfreeman"])
    attested = runner.invoke(
        cli.app, ["key", "attest", "claude", "--owner", "jfreeman", "--hours", "2"]
    )
    shown = runner.invoke(cli.app, ["key", "show", "claude"])
    assert attested.exit_code == 0, attested.output
    assert "authorized by @jfreeman" in shown.output
    assert "valid until" in shown.output


def test_key_attest_help_documents_the_npub_confirmation() -> None:
    result = runner.invoke(cli.app, ["key", "attest", "--help"])
    assert "--npub" in result.output


def _keyring_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_home: Path) -> tuple[SecretVault, SecretVault]:
        msg = "agent keys live in the OS keyring, which is not available"
        raise VaultError(msg)

    monkeypatch.setattr(cli_key, "machine_vaults", refuse)


def _publish_claude(tmp_path: Path) -> str:
    npub = Bech32Key.PUBLIC.encode(NostrCrypto().public_key(bytes([3]) * 32))
    local = tmp_path / "home" / ".punt-labs" / "biff" / "nostr.yaml"
    NostrDirectory(None, local).publish("claude", {"npub": npub})
    return npub


def test_key_show_works_when_the_recorded_keyring_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npub = _publish_claude(tmp_path)
    _keyring_unavailable(monkeypatch)
    result = runner.invoke(cli.app, ["key", "show", "claude"])
    assert result.exit_code == 0, result.output
    assert npub in result.output


def test_key_agent_still_refuses_when_the_recorded_keyring_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _keyring_unavailable(monkeypatch)
    result = runner.invoke(cli.app, ["key", "agent", "claude"])
    assert result.exit_code == 1
    assert "OS keyring" in result.output


def test_key_init_still_refuses_when_the_recorded_keyring_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _keyring_unavailable(monkeypatch)
    result = runner.invoke(cli.app, ["key", "init", "--owner", "jfreeman"])
    assert result.exit_code == 1
    assert "OS keyring" in result.output


def _attested_claude() -> None:
    runner.invoke(cli.app, ["key", "init", "--owner", "jfreeman"])
    attested = runner.invoke(
        cli.app, ["key", "attest", "claude", "--owner", "jfreeman", "--hours", "2"]
    )
    assert attested.exit_code == 0, attested.output


def test_key_show_verifies_an_attestation_with_the_keyring_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _attested_claude()
    _keyring_unavailable(monkeypatch)
    result = runner.invoke(cli.app, ["key", "show", "claude"])
    assert result.exit_code == 0, result.output
    assert "matches the owner key pinned on this machine" in result.output
    assert "valid until" in result.output


def test_key_show_is_unverified_when_the_owner_record_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _attested_claude()
    records = tmp_path / "home" / ".punt-labs" / "biff" / "keys" / "owner"
    shutil.rmtree(records)
    records.write_text("in the way")
    _keyring_unavailable(monkeypatch)
    result = runner.invoke(cli.app, ["key", "show", "claude"])
    assert result.exit_code == 0, result.output
    assert "unverified" in result.output
    assert "valid until" not in result.output


def test_key_show_json_reports_the_owner_key_verdict() -> None:
    _attested_claude()
    result = runner.invoke(cli.app, ["--json", "key", "show", "claude"])
    assert json.loads(result.output)["attestation"]["state"] == "valid"
