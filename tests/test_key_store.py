"""Key custody: who may read which private key, and what is logged."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from biff.key_store import CallerKind, KeyStore, OwnerKeyDeniedError
from biff.key_vault import PassphraseFileVault, VaultError
from biff.nostr_crypto import NostrCrypto


class _MemoryVault:
    """A vault that remembers every read, so a test can say none happened."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.reads: list[tuple[str, str]] = []

    def get(self, service: str, account: str) -> str | None:
        self.reads.append((service, account))
        return self.items.get((service, account))

    def has(self, service: str, account: str) -> bool:
        return (service, account) in self.items

    def put(self, service: str, account: str, secret: str) -> None:
        self.items[service, account] = secret


def _store(
    caller: CallerKind = CallerKind.HUMAN,
    owner: _MemoryVault | None = None,
    agents: _MemoryVault | None = None,
) -> KeyStore:
    return KeyStore(owner or _MemoryVault(), agents or _MemoryVault(), caller)


def test_agent_key_is_created_once_and_stable() -> None:
    store = _store()
    assert store.agent_pubkey("claude") == store.agent_pubkey("claude")


def test_each_agent_has_its_own_key() -> None:
    store = _store()
    assert store.agent_pubkey("claude") != store.agent_pubkey("rmh")


def test_agent_pubkey_matches_the_stored_secret() -> None:
    agents = _MemoryVault()
    store = _store(agents=agents)
    pubkey = store.agent_pubkey("claude")
    secret = bytes.fromhex(agents.items["biff-agent", "claude"])
    assert NostrCrypto().public_key(secret) == pubkey


def test_agent_key_read_is_logged_without_the_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    agents = _MemoryVault()
    store = _store(agents=agents)
    store.agent_pubkey("claude")
    with caplog.at_level(logging.INFO, logger="biff.key_store"):
        secret = store.agent_secret("claude", purpose="sign message")
    text = caplog.text
    assert "claude" in text
    assert "sign message" in text
    assert secret.hex() not in text


def test_owner_key_is_created_for_a_human() -> None:
    owner = _MemoryVault()
    pubkey = _store(owner=owner).init_owner("jfreeman")
    secret = bytes.fromhex(owner.items["biff-owner", "jfreeman"])
    assert NostrCrypto().public_key(secret) == pubkey


def test_owner_key_is_not_replaced_by_a_second_init() -> None:
    store = _store()
    store.init_owner("jfreeman")
    with pytest.raises(ValueError, match="already"):
        store.init_owner("jfreeman")


def test_agent_process_cannot_create_an_owner_key() -> None:
    owner = _MemoryVault()
    with pytest.raises(OwnerKeyDeniedError):
        _store(CallerKind.AGENT, owner=owner).init_owner("jfreeman")
    assert owner.items == {}


def test_agent_process_cannot_sign_an_attestation_and_never_reads_the_owner() -> None:
    owner = _MemoryVault()
    _store(owner=owner).init_owner("jfreeman")
    owner.reads.clear()
    agent_store = _store(CallerKind.AGENT, owner=owner)
    with pytest.raises(OwnerKeyDeniedError):
        agent_store.attest("jfreeman", "claude", lifetime_seconds=60, now=0)
    assert owner.reads == []


def test_owner_signing_prompts_for_the_passphrase_each_time(tmp_path: Path) -> None:
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "pw"

    store = KeyStore(
        PassphraseFileVault(tmp_path, ask), _MemoryVault(), CallerKind.HUMAN
    )
    store.init_owner("jfreeman")
    prompts.clear()
    store.attest("jfreeman", "claude", lifetime_seconds=60, now=0)
    store.attest("jfreeman", "claude", lifetime_seconds=60, now=0)
    assert len(prompts) == 2


def test_owner_signing_fails_without_the_right_passphrase(tmp_path: Path) -> None:
    right = KeyStore(
        PassphraseFileVault(tmp_path, lambda _p: "right"),
        _MemoryVault(),
        CallerKind.HUMAN,
    )
    right.init_owner("jfreeman")
    wrong = KeyStore(
        PassphraseFileVault(tmp_path, lambda _p: "wrong"),
        _MemoryVault(),
        CallerKind.HUMAN,
    )
    with pytest.raises(VaultError, match="passphrase"):
        wrong.attest("jfreeman", "claude", lifetime_seconds=60, now=0)


def test_attestation_names_the_agents_public_key_and_expires() -> None:
    store = _store()
    store.init_owner("jfreeman")
    agent = store.agent_pubkey("claude").hex()
    attestation = store.attest("jfreeman", "claude", lifetime_seconds=3600, now=1000)
    assert attestation.is_live(agent, now=1000)
    assert attestation.expires_at() == 4600


def test_agent_secret_is_not_the_owner_secret() -> None:
    owner, agents = _MemoryVault(), _MemoryVault()
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
