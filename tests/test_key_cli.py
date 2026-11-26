"""``biff key`` through the real command line entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import biff.__main__ as cli
from biff import cli_key, key_store
from biff.key_vault import SecretVault
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
