"""``biff key``: create keys, publish the public half, attest agents."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

from biff.commands.key import KeyCommands
from biff.key_store import CallerKind, KeyStore
from biff.nostr_attest import OwnerAttestation
from biff.nostr_bech32 import Bech32Key
from biff.nostr_crypto import NostrCrypto
from biff.nostr_directory import NostrDirectory
from biff.owner_records import OwnerRecords
from biff.passphrase_vault import PassphraseFileVault
from tests._memory_vault import MemoryVault

if TYPE_CHECKING:
    import pytest

NOW = 1_800_000_000
NPUB_RE = re.compile(r"npub1[a-z0-9]{58}")


class _Ethos:
    """An ethos ext store in memory; ``refuse`` and ``unreachable`` inject failures."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}
        self.refuse = False
        self.unreachable = False

    def get(self, handle: str) -> dict[str, str]:
        if self.unreachable:
            msg = "ethos ext get failed (exit 3): store corrupt"
            raise OSError(msg)
        return dict(self.data.get(handle, {}))

    def set(self, handle: str, key: str, value: str) -> None:
        if self.refuse:
            msg = f"ethos ext set {handle} nostr {key} failed: refused"
            raise OSError(msg)
        self.data.setdefault(handle, {})[key] = value


class _Rig:
    """A KeyCommands over fake vaults, a fake ethos and a fixed clock."""

    def __init__(
        self,
        tmp_path: Path,
        caller: CallerKind = CallerKind.HUMAN,
        *,
        with_ethos: bool = True,
        now: int = NOW,
    ) -> None:
        self.owner_vault, self.agent_vault = MemoryVault(), MemoryVault()
        self.ethos = _Ethos()
        self.directory = NostrDirectory(
            self.ethos if with_ethos else None, tmp_path / "nostr.yaml"
        )
        self.records_dir = tmp_path / "records"
        self.store = KeyStore(
            self.owner_vault,
            self.agent_vault,
            caller,
            OwnerRecords(self.records_dir),
        )
        self.commands = KeyCommands(self.store, self.directory, lambda: float(now))
        self.clock = now

    def later(self, now: int) -> KeyCommands:
        return KeyCommands(self.store, self.directory, lambda: float(now))


def _expiry(rig: _Rig, agent: str) -> int:
    conditions = rig.directory.fields(agent)["auth_conditions"]
    return int(conditions.split("<")[1])


def test_init_prints_the_npub(tmp_path: Path) -> None:
    result = _Rig(tmp_path).commands.init("jfreeman")
    assert re.search(rf"your npub: {NPUB_RE.pattern}", result.text)


def test_init_tells_the_user_never_to_click_always_allow(tmp_path: Path) -> None:
    result = _Rig(tmp_path).commands.init("jfreeman")
    assert "never click Always Allow" in result.text


def test_init_publishes_the_npub_through_ethos(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    assert list(rig.ethos.data["jfreeman"]) == ["npub"]
    assert NPUB_RE.fullmatch(rig.ethos.data["jfreeman"]["npub"])


def test_init_with_ethos_absent_uses_the_local_yaml(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, with_ethos=False)
    rig.commands.init("jfreeman")
    local = yaml.safe_load((tmp_path / "nostr.yaml").read_text())
    assert NPUB_RE.fullmatch(local["jfreeman"]["nostr"]["npub"])


def test_init_reports_a_publish_failure_after_storing_the_key(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.ethos.refuse = True
    result = rig.commands.init("jfreeman")
    assert result.error
    assert "owner key stored; publishing npub failed" in result.text
    assert "published nowhere" in result.text
    assert ("biff-owner", "jfreeman") in rig.owner_vault.items


def test_init_again_republishes_a_key_that_was_never_published(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.ethos.refuse = True
    rig.commands.init("jfreeman")
    rig.ethos.refuse = False
    result = rig.commands.init("jfreeman")
    assert not result.error
    assert rig.ethos.data["jfreeman"]["npub"] == Bech32Key.PUBLIC.encode(
        rig.store.owner_pubkey("jfreeman")
    )


def test_init_again_makes_no_second_owner_key(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    before = dict(rig.owner_vault.items)
    rig.commands.init("jfreeman")
    assert rig.owner_vault.items == before


def test_init_again_records_a_public_key_that_was_never_recorded(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    secret = rig.owner_vault.items["biff-owner", "jfreeman"]
    (rig.records_dir / "jfreeman.pub").unlink()
    result = rig.commands.init("jfreeman")
    assert not result.error
    assert rig.owner_vault.items["biff-owner", "jfreeman"] == secret
    recorded = rig.store.owner_pubkey("jfreeman")
    assert NostrCrypto().public_key(bytes.fromhex(secret)) == recorded


def test_init_recovers_when_recording_the_public_key_failed(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.records_dir.write_text("in the way")
    assert rig.commands.init("jfreeman").error
    rig.records_dir.unlink()
    result = rig.commands.init("jfreeman")
    assert not result.error
    assert NPUB_RE.fullmatch(rig.ethos.data["jfreeman"]["npub"])


def test_init_from_an_agent_process_is_refused(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, CallerKind.AGENT)
    result = rig.commands.init("jfreeman")
    assert result.error
    assert rig.owner_vault.items == {}


def test_agent_key_is_published_and_names_the_vault(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    result = rig.commands.agent("claude")
    npub = rig.directory.npub("claude")
    assert npub is not None
    assert npub in result.text
    assert "memory" in result.text


def test_agent_publish_failure_is_an_error(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.ethos.refuse = True
    result = rig.commands.agent("claude")
    assert result.error
    assert "published nowhere" in result.text


def test_agent_command_works_from_an_agent_process(tmp_path: Path) -> None:
    result = _Rig(tmp_path, CallerKind.AGENT).commands.agent("claude")
    assert not result.error


def test_agent_vault_failure_is_an_error_result(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.agent("claude")
    rig.agent_vault.fail_get = True
    assert rig.commands.agent("claude").error


def test_attest_publishes_the_signed_terms_and_the_owner_handle(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.agent("claude")
    result = rig.commands.attest("jfreeman", "claude", hours=24)
    fields = rig.directory.fields("claude")
    assert not result.error
    assert set(fields) == {"npub", "owner", "auth_conditions", "auth_sig"}
    assert fields["owner"] == "jfreeman"


def test_attest_publishes_nothing_the_signature_does_not_cover(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    assert "auth_expires_at" not in rig.directory.fields("claude")
    assert "owner_npub" not in rig.directory.fields("claude")


def test_attest_expires_after_the_requested_hours(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=24)
    assert _expiry(rig, "claude") == NOW + 86_400


def test_attest_from_an_agent_process_is_refused_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    agent = _Rig(tmp_path / "agent", CallerKind.AGENT, with_ethos=False)
    result = agent.commands.attest("jfreeman", "claude", hours=24)
    assert result.error
    assert agent.directory.fields("claude") == {}


def test_attest_default_lifetime_is_a_day_on_a_developer_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIFF_ATTESTATION_HOURS", raising=False)
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude")
    assert _expiry(rig, "claude") == NOW + 24 * 3600


def test_attest_for_an_agent_image_defaults_to_a_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIFF_ATTESTATION_HOURS", raising=False)
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", image=True)
    assert _expiry(rig, "claude") == NOW + 7 * 86_400


def test_attest_lifetime_is_configurable_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_ATTESTATION_HOURS", "2")
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude")
    assert _expiry(rig, "claude") == NOW + 7200


def test_attest_rejects_a_malformed_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_ATTESTATION_HOURS", "soon")
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    assert rig.commands.attest("jfreeman", "claude").error


def test_a_locked_owner_vault_is_an_error_result_not_a_traceback(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.owner_vault.fail_get = True
    result = rig.commands.attest("jfreeman", "claude", hours=1)
    assert result.error
    assert "locked" in result.text


def test_attest_with_a_swapped_owner_key_is_an_error_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.owner_vault.items["biff-owner", "jfreeman"] = (
        NostrCrypto().generate_secret().hex()
    )
    result = rig.commands.attest("jfreeman", "claude", hours=1)
    assert result.error
    assert "claude" not in rig.ethos.data


def test_attest_publish_failure_is_an_error(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.ethos.refuse = True
    result = rig.commands.attest("jfreeman", "claude", hours=1)
    assert result.error
    assert "published nowhere" in result.text


def test_show_reports_the_owner_by_handle_and_the_attestation_state(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    text = rig.commands.show("claude").text
    assert "authorized by @jfreeman" in text
    assert "valid until" in text


def test_show_marks_an_expired_attestation(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    assert "expired" in rig.later(NOW + 7200).show("claude").text


def test_show_does_not_trust_an_owner_the_agent_named(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    # An agent rewrites the namespace to claim a different owner.
    mallory = Bech32Key.PUBLIC.encode(NostrCrypto().public_key(bytes([7]) * 32))
    rig.directory.publish("mallory", {"npub": mallory})
    rig.directory.publish("claude", {"owner": "mallory"})
    text = rig.commands.show("claude").text
    assert "invalid" in text
    assert "valid until" not in text


def test_show_reports_an_owner_with_no_published_key(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    rig.directory.publish("claude", {"owner": "ghost"})
    assert "no published key" in rig.commands.show("claude").text


def test_show_of_an_unknown_handle_is_an_error(tmp_path: Path) -> None:
    assert _Rig(tmp_path).commands.show("nobody").error


def test_show_is_loud_when_ethos_cannot_be_asked(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.ethos.unreachable = True
    result = rig.commands.show("claude")
    assert result.error
    assert "store corrupt" in result.text


def test_show_of_a_handle_with_an_at_sign_is_an_error_result(tmp_path: Path) -> None:
    result = _Rig(tmp_path).commands.show("@jfreeman")
    assert result.error
    assert "invalid handle" in result.text


def _published_elsewhere(rig: _Rig, agent: str) -> bytes:
    """Publish a key for *agent* as if its own host had made it; return the key."""
    pubkey = NostrCrypto().public_key(bytes([9]) * 32)
    rig.directory.publish(agent, {"npub": Bech32Key.PUBLIC.encode(pubkey)})
    return pubkey


def test_attest_signs_the_published_key_not_a_replacement(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    published = _published_elsewhere(rig, "claude")
    npub = Bech32Key.PUBLIC.encode(published)
    result = rig.commands.attest("jfreeman", "claude", image=True, npub=npub)
    assert not result.error, result.text
    assert rig.directory.npub("claude") == npub


def test_attest_of_a_published_key_creates_no_local_agent_key(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    published = _published_elsewhere(rig, "claude")
    npub = Bech32Key.PUBLIC.encode(published)
    rig.commands.attest("jfreeman", "claude", image=True, npub=npub)
    assert ("biff-agent", "claude") not in rig.agent_vault.items


def test_attestation_of_a_published_key_verifies_against_that_key(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    published = _published_elsewhere(rig, "claude")
    npub = Bech32Key.PUBLIC.encode(published)
    rig.commands.attest("jfreeman", "claude", hours=1, npub=npub)
    assert "valid until" in rig.commands.show("claude").text
    assert rig.directory.fields("claude")["npub"] == Bech32Key.PUBLIC.encode(published)


def test_attest_refuses_when_the_local_key_differs_from_the_published_one(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.agent("claude")
    published = _published_elsewhere(rig, "claude")
    result = rig.commands.attest("jfreeman", "claude", hours=1)
    assert result.error
    assert "differs" in result.text
    assert rig.directory.npub("claude") == Bech32Key.PUBLIC.encode(published)
    assert "auth_sig" not in rig.directory.fields("claude")


def test_attest_of_a_directory_key_without_npub_refuses_and_signs_nothing(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    published = _published_elsewhere(rig, "claude")
    result = rig.commands.attest("jfreeman", "claude", hours=1)
    assert result.error
    assert Bech32Key.PUBLIC.encode(published) in result.text
    assert "--npub" in result.text
    assert "auth_sig" not in rig.directory.fields("claude")


def test_attest_of_a_directory_key_with_a_different_npub_refuses(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    published = _published_elsewhere(rig, "claude")
    other = Bech32Key.PUBLIC.encode(NostrCrypto().public_key(bytes([5]) * 32))
    result = rig.commands.attest("jfreeman", "claude", hours=1, npub=other)
    assert result.error
    assert Bech32Key.PUBLIC.encode(published) in result.text
    assert "auth_sig" not in rig.directory.fields("claude")


def test_attest_with_a_malformed_npub_refuses(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    _published_elsewhere(rig, "claude")
    result = rig.commands.attest("jfreeman", "claude", hours=1, npub="npub1nope")
    assert result.error
    assert "auth_sig" not in rig.directory.fields("claude")


def test_attest_of_a_local_key_needs_no_npub(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.agent("claude")
    assert not rig.commands.attest("jfreeman", "claude", hours=1).error


def test_attest_of_a_local_key_refuses_a_different_npub(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.agent("claude")
    other = Bech32Key.PUBLIC.encode(NostrCrypto().public_key(bytes([5]) * 32))
    assert rig.commands.attest("jfreeman", "claude", hours=1, npub=other).error


def _forge(rig: _Rig, agent: str, secret: bytes) -> None:
    """Publish an attestation of *agent* signed by *secret*, as an attacker would."""
    agent_hex = Bech32Key.PUBLIC.decode(rig.directory.fields(agent)["npub"]).hex()
    forged = OwnerAttestation.mint(secret, agent_hex, now=NOW, lifetime_seconds=3600)
    rig.directory.publish(
        agent,
        {"auth_conditions": forged.conditions, "auth_sig": forged.signature},
    )


def test_show_rejects_an_owner_key_swapped_in_the_directory(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.agent("claude")
    attacker = bytes([7]) * 32
    rig.directory.publish(
        "jfreeman",
        {"npub": Bech32Key.PUBLIC.encode(NostrCrypto().public_key(attacker))},
    )
    rig.directory.publish("claude", {"owner": "jfreeman"})
    _forge(rig, "claude", attacker)
    text = rig.commands.show("claude").text
    assert "invalid" in text
    assert "valid until" not in text


def test_show_says_when_the_owner_key_is_recorded_on_this_machine(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    assert "recorded on this machine" in rig.commands.show("claude").text


def test_show_says_when_the_owner_key_is_only_published(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.agent("claude")
    stranger = bytes([7]) * 32
    rig.directory.publish(
        "mallory", {"npub": Bech32Key.PUBLIC.encode(NostrCrypto().public_key(stranger))}
    )
    rig.directory.publish("claude", {"owner": "mallory"})
    _forge(rig, "claude", stranger)
    text = rig.commands.show("claude").text
    assert "not recorded on this machine" in text
    assert "npub1" in text


def test_show_names_the_owner_key_it_checked(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    owner_npub = Bech32Key.PUBLIC.encode(rig.store.owner_pubkey("jfreeman"))
    assert Bech32Key.abbreviate(owner_npub) in rig.commands.show("claude").text


def test_show_is_unverified_when_the_recorded_owner_key_is_unavailable(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    shutil.rmtree(rig.records_dir)
    rig.records_dir.write_text("in the way")
    result = rig.commands.show("claude")
    assert not result.error
    assert (
        "authorized by @jfreeman: unverified, "
        "recorded owner key unreadable in this session"
    ) in result.text
    assert "valid until" not in result.text


def test_show_calls_a_corrupt_recorded_owner_key_invalid_not_unreadable(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    (rig.records_dir / "jfreeman.pub").write_text("not hex at all")
    text = rig.commands.show("claude").text
    assert "invalid" in text
    assert "unverified" not in text
    assert "valid until" not in text


def test_show_verifies_on_a_headless_linux_machine_without_a_prompt(
    tmp_path: Path,
) -> None:
    keys = tmp_path / "keys"
    ethos = _Ethos()
    directory = NostrDirectory(ethos, tmp_path / "nostr.yaml")

    def sealed_store(ask: Callable[[str], str]) -> KeyStore:
        return KeyStore(
            PassphraseFileVault(keys / "owner", ask),
            PassphraseFileVault(keys / "agent", ask),
            CallerKind.HUMAN,
            OwnerRecords(keys / "owner"),
        )

    def forbidden(prompt: str) -> str:
        msg = f"show asked for a passphrase: {prompt}"
        raise AssertionError(msg)

    typing = KeyCommands(
        sealed_store(lambda _p: "a long enough passphrase"),
        directory,
        lambda: float(NOW),
    )
    typing.init("jfreeman")
    typing.attest("jfreeman", "claude", hours=1)
    text = KeyCommands(sealed_store(forbidden), directory, lambda: float(NOW)).show(
        "claude"
    )
    assert "recorded on this machine" in text.text
    assert "valid until" in text.text


def _show_json(rig: _Rig) -> dict[str, object]:
    data = rig.commands.show("claude").json_data
    assert isinstance(data, dict)
    return cast("dict[str, object]", data)


def test_show_json_carries_the_owner_key_verdict(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    data = _show_json(rig)
    assert data["attestation"] == {
        "owner": "jfreeman",
        "state": "valid",
        "standing": "recorded on this machine",
    }


def test_show_json_marks_an_unreadable_owner_record_unverified(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.commands.init("jfreeman")
    rig.commands.attest("jfreeman", "claude", hours=1)
    shutil.rmtree(rig.records_dir)
    rig.records_dir.write_text("in the way")
    attestation = cast("dict[str, str]", _show_json(rig)["attestation"])
    assert attestation["state"] == "unverified"


def test_show_json_has_no_verdict_for_an_unattested_key(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    rig.commands.agent("claude")
    assert "attestation" not in _show_json(rig)
