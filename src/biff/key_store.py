"""Custody of private keys: the owner key stays out of agents' reach."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Self, final

from biff.key_vault import SecretVault, VaultError, VaultKey
from biff.nostr_attest import OwnerAttestation
from biff.nostr_crypto import NostrCrypto

logger = logging.getLogger(__name__)

OWNER_SERVICE = "biff-owner"
AGENT_SERVICE = "biff-agent"
OWNER_PUBLIC_SERVICE = "biff-owner-pub"
AGENT_IDENTITY_MOUNT = Path("/run/agent-identity")
_AGENT_ENV_MARKERS = ("CLAUDECODE", "OPENCODE")
_KEY_BYTES = 32


class OwnerKeyDeniedError(PermissionError):
    """An agent process asked to create or use the owner key."""


class KeyNotFoundError(LookupError):
    """No key is stored under the name asked for."""


class KeyExistsError(ValueError):
    """A key already exists under the name a caller tried to create."""


class OwnerKeyMismatchError(Exception):
    """The released owner secret is not the key recorded when the owner started."""


class CallerKind(Enum):
    """Whether the process running biff is a person's shell or an agent's."""

    HUMAN = "human"
    AGENT = "agent"

    @classmethod
    def detect(
        cls, env: Mapping[str, str], agent_identity_mount: Path | None = None
    ) -> Self:
        """Classify the process by the markers a coding agent leaves.

        Claude Code and opencode export a marker variable to every command
        they run, and an agent image mounts the agent's identity.  This is a
        refusal biff makes on its own; it does not stop a process that hides
        its markers.  The owner vault does: it asks the person.
        """
        if any(env.get(name) for name in _AGENT_ENV_MARKERS):
            return cls("agent")
        mount = agent_identity_mount or AGENT_IDENTITY_MOUNT
        return cls("agent" if mount.is_dir() else "human")

    @classmethod
    def current(cls) -> Self:
        """Classify the running process."""
        return cls.detect(os.environ)


@final
class KeyStore:
    """Creates keys and releases them according to who is asking.

    The owner vault releases a key only after the person is present: the
    keychain dialog on macOS, the passphrase elsewhere.  That proves a person
    was present.  It does not prove which process asked or what gets signed,
    so an agent that can start ``biff key attest`` in the person's own shell
    could ask for a signature while the person is watching.  Before signing,
    ``attest`` derives the public key from the released secret and compares it
    with the one recorded when the owner key was created, so a swapped vault
    item signs nothing.  The store never keeps a secret between calls.  An
    agent's own key sits in an ordinary vault, and every read of it is logged.
    """

    __slots__ = ("_agents", "_caller", "_crypto", "_owner")

    _owner: SecretVault
    _agents: SecretVault
    _caller: CallerKind
    _crypto: NostrCrypto

    def __new__(
        cls, owner_vault: SecretVault, agent_vault: SecretVault, caller: CallerKind
    ) -> Self:
        """Keep owner keys in *owner_vault* and agent keys in *agent_vault*."""
        self = super().__new__(cls)
        self._owner = owner_vault
        self._agents = agent_vault
        self._caller = caller
        self._crypto = NostrCrypto()
        return self

    @property
    def agent_vault_label(self) -> str:
        """Name the vault that holds agent keys, for a message to the user."""
        return self._agents.label

    def init_owner(self, handle: str) -> bytes:
        """Create *handle*'s owner key and return its public key.

        The public half is recorded beside the agent keys, where reading it
        asks nothing of the person: a later ``attest`` checks the released
        secret against it, and a republish needs no secret at all.
        """
        self._require_human("create the owner key")
        secret_key = VaultKey(OWNER_SERVICE, handle)
        record_key = VaultKey(OWNER_PUBLIC_SERVICE, handle)
        if self._owner.has(secret_key):
            if self._agents.has(record_key):
                msg = f"owner key for {handle} already exists"
                raise KeyExistsError(msg)
            return self._record_existing_owner(handle)
        secret = self._crypto.generate_secret()
        self._owner.put(secret_key, secret.hex())
        pubkey = self._crypto.public_key(secret)
        self._agents.put(record_key, pubkey.hex())
        logger.info("key created: role=owner handle=%s", handle)
        return pubkey

    def owner_pubkey(self, handle: str) -> bytes:
        """Return the public key recorded when *handle*'s owner key was created."""
        key = VaultKey(OWNER_PUBLIC_SERVICE, handle)
        stored = self._agents.get(key)
        if stored is None:
            msg = f"no recorded owner key for {handle}; run `biff key init`"
            raise KeyNotFoundError(msg)
        return self._decode(stored, key, self._agents)

    def agent_pubkey(self, handle: str) -> bytes:
        """Return the agent's public key, creating its key on first use."""
        existing = self.existing_agent_pubkey(handle)
        if existing is not None:
            return existing
        secret = self._crypto.generate_secret()
        self._agents.put(VaultKey(AGENT_SERVICE, handle), secret.hex())
        logger.info("key created: role=agent handle=%s", handle)
        return self._crypto.public_key(secret)

    def existing_agent_pubkey(self, handle: str) -> bytes | None:
        """Return the public key of the agent's key, or ``None`` when it has none."""
        if not self._agents.has(VaultKey(AGENT_SERVICE, handle)):
            return None
        return self._crypto.public_key(self.agent_secret(handle, purpose="derive"))

    def agent_secret(self, handle: str, *, purpose: str) -> bytes:
        """Return the agent's secret key, logging who read it and why."""
        key = VaultKey(AGENT_SERVICE, handle)
        stored = self._agents.get(key)
        logger.info(
            "key access: role=agent handle=%s purpose=%s pid=%d",
            handle,
            purpose,
            os.getpid(),
        )
        if stored is None:
            msg = f"no key for agent {handle}"
            raise KeyNotFoundError(msg)
        return self._decode(stored, key, self._agents)

    def attest(
        self, owner: str, agent_pubkey: bytes, *, lifetime_seconds: int, now: int
    ) -> OwnerAttestation:
        """Sign *agent_pubkey* as *owner*; the owner must be present.

        The key is whatever the caller names, so an agent whose private key
        lives on another host is attested without minting a second one here.
        """
        self._require_human("sign an attestation")
        recorded = self.owner_pubkey(owner)
        key = VaultKey(OWNER_SERVICE, owner)
        stored = self._owner.get(key)
        if stored is None:
            msg = f"no owner key for {owner}; run `biff key init`"
            raise KeyNotFoundError(msg)
        secret = self._decode(stored, key, self._owner)
        logger.info("key access: role=owner handle=%s purpose=attest", owner)
        if self._crypto.public_key(secret) != recorded:
            msg = (
                f"the owner key released for {owner} is not the key recorded at "
                "`biff key init`; nothing was signed"
            )
            raise OwnerKeyMismatchError(msg)
        return OwnerAttestation.mint(
            secret, agent_pubkey.hex(), now=now, lifetime_seconds=lifetime_seconds
        )

    def _record_existing_owner(self, handle: str) -> bytes:
        """Record the public key of an owner secret whose record never landed."""
        key = VaultKey(OWNER_SERVICE, handle)
        stored = self._owner.get(key)
        if stored is None:
            msg = f"no owner key for {handle}; run `biff key init`"
            raise KeyNotFoundError(msg)
        pubkey = self._crypto.public_key(self._decode(stored, key, self._owner))
        self._agents.put(VaultKey(OWNER_PUBLIC_SERVICE, handle), pubkey.hex())
        logger.info("key repaired: role=owner handle=%s public record written", handle)
        return pubkey

    @staticmethod
    def _decode(stored: str, key: VaultKey, vault: SecretVault) -> bytes:
        """Return the 32 bytes in *stored*, or raise naming where they came from."""
        try:
            raw = bytes.fromhex(stored)
        except ValueError:
            raw = b""
        if len(raw) != _KEY_BYTES:
            msg = (
                f"the value stored for {key.where} in {vault.label} "
                "is not a 32-byte hex key"
            )
            raise VaultError(msg)
        return raw

    def _require_human(self, action: str) -> None:
        if self._caller is not CallerKind.HUMAN:
            msg = f"an agent process may not {action}; run it from your own shell"
            raise OwnerKeyDeniedError(msg)
