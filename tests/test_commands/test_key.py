"""``biff key``: create keys, publish the public half, attest agents."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from biff.commands.key import KeyCommands
from biff.key_store import CallerKind, KeyStore
from biff.nostr_attest import OwnerAttestation
from biff.nostr_crypto import NostrCrypto
from biff.nostr_directory import NostrDirectory

if TYPE_CHECKING:
    import pytest

NOW = 1_800_000_000


class _Vault:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get(self, service: str, account: str) -> str | None:
        return self.items.get((service, account))

    def has(self, service: str, account: str) -> bool:
        return (service, account) in self.items

    def put(self, service: str, account: str, secret: str) -> None:
        self.items[service, account] = secret


class _Ethos:
    """An ethos ext store in memory."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}

    def get(self, handle: str) -> dict[str, str]:
        return dict(self.data.get(handle, {}))

    def set(self, handle: str, key: str, value: str) -> None:
        self.data.setdefault(handle, {})[key] = value


class _Rig:
    """A KeyCommands over fake vaults, a fake ethos and a fixed clock."""

    def __init__(
        self,
        tmp_path: Path,
        caller: CallerKind = CallerKind.HUMAN,
        *,
        with_ethos: bool = True,
    ) -> None:
        self.owner_vault, self.agent_vault = _Vault(), _Vault()
        self.ethos = _Ethos()
        self.directory = NostrDirectory(
            self.ethos if with_ethos else None, tmp_path / "nostr.yaml"
        )
        store = KeyStore(self.owner_vault, self.agent_vault, caller)
        self.commands = KeyCommands(store, self.directory, lambda: float(NOW))
        self.tmp_path = tmp_path


def test_init_prints_the_npub(tmp_path: Path) -> None:
    result = _Rig(tmp_path).commands.init("jfreeman")
    assert re.fullmatch(r"your npub: npub1[a-z0-9]{58}", result.text)


def test_init_publishes_the_npub_through_ethos(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    result = rig.commands.init("jfreeman")
    assert rig.ethos.data == {"jfreeman": {"npub": result.text.split()[-1]}}


def test_init_with_ethos_absent_uses_the_local_yaml(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, with_ethos=False)
    result = rig.commands.init("jfreeman")
    local = yaml.safe_load((tmp_path / "nostr.yaml").read_text())
    assert local == {"jfreeman": {"nostr": {"npub": result.text.split()[-1]}}}


def test_init_twice_is_an_error_result(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    assert rig.commands.init("jfreeman").error


def test_init_from_an_agent_process_is_refused(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, CallerKind.AGENT)
    result = rig.commands.init("jfreeman")
    assert result.error
    assert rig.owner_vault.items == {}


def test_agent_key_is_published(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    result = rig.commands.agent("claude")
    assert rig.directory.npub("claude") == result.text.split()[-1]


def test_agent_command_works_from_an_agent_process(tmp_path: Path) -> None:
    result = _Rig(tmp_path, CallerKind.AGENT).commands.agent("claude")
    assert not result.error


def test_attest_publishes_a_verifiable_attestation(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.agent("claude")
    result = rig.commands.attest("jfreeman", "claude", hours=24)
    fields = rig.directory.fields("claude")
    crypto = NostrCrypto()
    agent_hex = crypto.decode_npub(fields["npub"]).hex()
    owner_hex = crypto.decode_npub(fields["owner_npub"]).hex()
    attestation = OwnerAttestation(
        owner_hex, fields["auth_conditions"], fields["auth_sig"]
    )
    assert not result.error
    assert attestation.is_live(agent_hex, now=NOW)


def test_attest_expires_after_the_requested_hours(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=24)
    assert rig.directory.fields("claude")["auth_expires_at"] == str(NOW + 86_400)


def test_attest_from_an_agent_process_is_refused_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    agent_rig = _Rig(tmp_path / "agent", CallerKind.AGENT, with_ethos=False)
    result = agent_rig.commands.attest("jfreeman", "claude", hours=24)
    assert result.error
    assert agent_rig.directory.fields("claude") == {}


def test_attest_default_lifetime_is_a_day_on_a_developer_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIFF_ATTESTATION_HOURS", raising=False)
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude")
    assert rig.directory.fields("claude")["auth_expires_at"] == str(NOW + 24 * 3600)


def test_attest_for_an_agent_image_defaults_to_a_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIFF_ATTESTATION_HOURS", raising=False)
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", image=True)
    assert rig.directory.fields("claude")["auth_expires_at"] == str(NOW + 7 * 86_400)


def test_attest_lifetime_is_configurable_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_ATTESTATION_HOURS", "2")
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude")
    assert rig.directory.fields("claude")["auth_expires_at"] == str(NOW + 7200)


def test_attest_rejects_a_malformed_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_ATTESTATION_HOURS", "soon")
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    assert rig.commands.attest("jfreeman", "claude").error


def test_show_reports_key_and_attestation_state(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    text = rig.commands.show("claude").text
    assert "authorized by" in text
    assert "valid" in text


def test_show_marks_an_expired_attestation(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    later = KeyCommands(
        KeyStore(rig.owner_vault, rig.agent_vault, CallerKind.HUMAN),
        rig.directory,
        lambda: float(NOW + 7200),
    )
    assert "expired" in later.show("claude").text


def test_show_of_an_unknown_handle_is_an_error(tmp_path: Path) -> None:
    assert _Rig(tmp_path).commands.show("nobody").error
