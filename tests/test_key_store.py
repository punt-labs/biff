"""Key custody: who may read which private key, and what is logged."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from biff.key_store import (
    CallerKind,
    KeyExistsError,
    KeyNotFoundError,
    KeyStore,
    OwnerKeyDeniedError,
    OwnerKeyMismatchError,
)
from biff.key_vault import VaultError
from biff.nostr_crypto import NostrCrypto
from biff.passphrase_vault import PassphraseFileVault
from tests._memory_vault import MemoryVault

PASS = "correct horse battery"
AGENT_KEY = bytes(31) + b"\x07"


def _store(
    caller: CallerKind = CallerKind.HUMAN,
    owner: MemoryVault | None = None,
    agents: MemoryVault | None = None,
) -> KeyStore:
    return KeyStore(owner or MemoryVault(), agents or MemoryVault(), caller)


def test_agent_key_is_created_once_and_stable() -> None:
    store = _store()
    assert store.agent_pubkey("claude") == store.agent_pubkey("claude")


def test_each_agent_has_its_own_key() -> None:
    store = _store()
    assert store.agent_pubkey("claude") != store.agent_pubkey("rmh")


def test_agent_pubkey_matches_the_stored_secret() -> None:
    agents = MemoryVault()
    store = _store(agents=agents)
    pubkey = store.agent_pubkey("claude")
    secret = bytes.fromhex(agents.items["biff-agent", "claude"])
    assert NostrCrypto().public_key(secret) == pubkey


def test_agent_key_read_is_logged_without_the_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    agents = MemoryVault()
    store = _store(agents=agents)
    store.agent_pubkey("claude")
    with caplog.at_level(logging.INFO, logger="biff.key_store"):
        secret = store.agent_secret("claude", purpose="sign message")
    assert "claude" in caplog.text
    assert "sign message" in caplog.text
    assert secret.hex() not in caplog.text


def test_a_missing_agent_key_is_a_key_not_found_error() -> None:
    with pytest.raises(KeyNotFoundError, match="claude"):
        _store().agent_secret("claude", purpose="test")


def test_key_not_found_is_a_lookup_error() -> None:
    assert issubclass(KeyNotFoundError, LookupError)


def test_a_corrupt_stored_secret_is_a_vault_error_naming_the_item() -> None:
    agents = MemoryVault()
    agents.items["biff-agent", "claude"] = "not hex at all"
    with pytest.raises(VaultError, match="biff-agent/claude"):
        _store(agents=agents).agent_secret("claude", purpose="test")


def test_a_stored_secret_of_the_wrong_length_is_a_vault_error() -> None:
    agents = MemoryVault()
    agents.items["biff-agent", "claude"] = "ab" * 31
    with pytest.raises(VaultError, match="biff-agent/claude"):
        _store(agents=agents).agent_secret("claude", purpose="test")


def test_the_store_names_the_vault_holding_agent_keys() -> None:
    assert _store().agent_vault_label == "memory"


def test_owner_key_is_created_for_a_human() -> None:
    owner = MemoryVault()
    pubkey = _store(owner=owner).init_owner("jfreeman")
    secret = bytes.fromhex(owner.items["biff-owner", "jfreeman"])
    assert NostrCrypto().public_key(secret) == pubkey


def test_the_owner_public_key_is_recorded_without_reading_the_secret() -> None:
    owner = MemoryVault()
    store = _store(owner=owner)
    pubkey = store.init_owner("jfreeman")
    owner.reads.clear()
    assert store.owner_pubkey("jfreeman") == pubkey
    assert owner.reads == []


def test_init_owner_completes_after_the_public_record_write_failed() -> None:
    owner, agents = MemoryVault(), MemoryVault()
    store = _store(owner=owner, agents=agents)
    agents.fail_put_after = 0
    with pytest.raises(VaultError):
        store.init_owner("jfreeman")
    secret = owner.items["biff-owner", "jfreeman"]
    agents.fail_put_after = None
    pubkey = store.init_owner("jfreeman")
    assert owner.items["biff-owner", "jfreeman"] == secret
    assert store.owner_pubkey("jfreeman") == pubkey
    assert NostrCrypto().public_key(bytes.fromhex(secret)) == pubkey


def test_an_owner_key_that_exists_is_not_replaced_by_a_second_init() -> None:
    store = _store()
    store.init_owner("jfreeman")
    with pytest.raises(KeyExistsError, match="already"):
        store.init_owner("jfreeman")


def test_no_recorded_owner_key_is_key_not_found() -> None:
    with pytest.raises(KeyNotFoundError, match="jfreeman"):
        _store().owner_pubkey("jfreeman")


def test_agent_process_cannot_create_an_owner_key() -> None:
    owner = MemoryVault()
    with pytest.raises(OwnerKeyDeniedError):
        _store(CallerKind.AGENT, owner=owner).init_owner("jfreeman")
    assert owner.items == {}


def test_agent_process_cannot_sign_an_attestation_and_never_reads_the_owner() -> None:
    owner = MemoryVault()
    _store(owner=owner).init_owner("jfreeman")
    owner.reads.clear()
    agent_store = _store(CallerKind.AGENT, owner=owner)
    with pytest.raises(OwnerKeyDeniedError):
        agent_store.attest("jfreeman", AGENT_KEY, lifetime_seconds=60, now=0)
    assert owner.reads == []


def test_owner_signing_prompts_for_the_passphrase_each_time(tmp_path: Path) -> None:
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return PASS

    store = KeyStore(
        PassphraseFileVault(tmp_path, ask), MemoryVault(), CallerKind.HUMAN
    )
    store.init_owner("jfreeman")
    prompts.clear()
    store.attest("jfreeman", AGENT_KEY, lifetime_seconds=60, now=0)
    store.attest("jfreeman", AGENT_KEY, lifetime_seconds=60, now=0)
    assert len(prompts) == 2


def test_owner_signing_fails_without_the_right_passphrase(tmp_path: Path) -> None:
    agents = MemoryVault()
    right = KeyStore(
        PassphraseFileVault(tmp_path, lambda _p: PASS), agents, CallerKind.HUMAN
    )
    right.init_owner("jfreeman")
    wrong = KeyStore(
        PassphraseFileVault(tmp_path, lambda _p: PASS + "x"), agents, CallerKind.HUMAN
    )
    with pytest.raises(VaultError, match="passphrase"):
        wrong.attest("jfreeman", AGENT_KEY, lifetime_seconds=60, now=0)


def test_attestation_names_the_agents_public_key_and_expires() -> None:
    store = _store()
    store.init_owner("jfreeman")
    agent = store.agent_pubkey("claude")
    attestation = store.attest("jfreeman", agent, lifetime_seconds=3600, now=1000)
    assert attestation.is_live(agent.hex(), now=1000)
    assert attestation.expires_at() == 4600


def test_attest_refuses_a_released_secret_that_is_not_the_recorded_key() -> None:
    owner = MemoryVault()
    store = _store(owner=owner)
    store.init_owner("jfreeman")
    owner.items["biff-owner", "jfreeman"] = NostrCrypto().generate_secret().hex()
    with pytest.raises(OwnerKeyMismatchError, match="jfreeman"):
        store.attest("jfreeman", AGENT_KEY, lifetime_seconds=60, now=0)


def test_attest_without_an_owner_key_is_key_not_found() -> None:
    with pytest.raises(KeyNotFoundError, match="jfreeman"):
        _store().attest("jfreeman", AGENT_KEY, lifetime_seconds=60, now=0)


def test_agent_secret_is_not_the_owner_secret() -> None:
    owner, agents = MemoryVault(), MemoryVault()
    store = _store(owner=owner, agents=agents)
    store.init_owner("jfreeman")
    store.agent_pubkey("claude")
    assert owner.items["biff-owner", "jfreeman"] != agents.items["biff-agent", "claude"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, CallerKind.HUMAN),
        ({"CLAUDECODE": "1"}, CallerKind.AGENT),
        ({"OPENCODE": "1"}, CallerKind.AGENT),
    ],
)
def test_caller_kind_is_read_from_the_environment(
    env: dict[str, str], expected: CallerKind, tmp_path: Path
) -> None:
    assert CallerKind.detect(env, tmp_path / "absent") is expected


def test_the_agent_image_identity_mount_marks_an_agent(tmp_path: Path) -> None:
    mount = tmp_path / "agent-identity"
    mount.mkdir()
    assert CallerKind.detect({}, mount) is CallerKind.AGENT
