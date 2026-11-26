"""The mutual challenge-response: each side proves the token without sending it.

The client sends a ``challenge`` with a fresh nonce.  The daemon answers with
its own fresh nonce and a MAC over both under the token, labelled as the
daemon's.  The client checks it, then sends a hello whose MAC covers both
nonces in the other order under the client's label.  The token never crosses
the wire, and a MAC is useful only on the one connection whose two nonces it
binds.  The labels keep either side's MAC from being replayed as the other's.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated, Final, Literal, final

from pydantic import StringConstraints

from biff.frame_base import FrameBase

__all__ = ["Challenge", "Handshake", "Nonce", "Proof"]

type Nonce = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]

_DAEMON_LABEL: Final = b"biffd-daemon-v1"
_CLIENT_LABEL: Final = b"biffd-client-v1"
_NONCE_BYTES: Final = 16


@final
class Handshake:
    """The MACs and nonces of the handshake."""

    @staticmethod
    def fresh_nonce() -> str:
        """Return a new 128-bit nonce as hex."""
        return secrets.token_hex(_NONCE_BYTES)

    @staticmethod
    def daemon_mac(token: str, client_nonce: str, daemon_nonce: str) -> str:
        """Return the MAC the daemon sends to prove it holds *token*."""
        return Handshake._mac(token, _DAEMON_LABEL, client_nonce, daemon_nonce)

    @staticmethod
    def client_mac(token: str, daemon_nonce: str, client_nonce: str) -> str:
        """Return the MAC the client sends in its hello to prove it holds *token*."""
        return Handshake._mac(token, _CLIENT_LABEL, daemon_nonce, client_nonce)

    @staticmethod
    def matches(mac: str, expected: str) -> bool:
        """Compare two MACs in constant time."""
        return hmac.compare_digest(mac.encode(), expected.encode())

    @staticmethod
    def _mac(token: str, label: bytes, first: str, second: str) -> str:
        message = label + first.encode() + second.encode()
        return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


class Challenge(FrameBase):
    """First frame of a connection: the client's fresh nonce.  Nothing secret."""

    type: Literal["challenge"] = "challenge"
    client_nonce: Nonce

    @classmethod
    def fresh(cls) -> Challenge:
        """Return a challenge with a new client nonce."""
        return cls(client_nonce=Handshake.fresh_nonce())


class Proof(FrameBase):
    """The daemon's answer: its own nonce and the MAC that proves it holds the token."""

    type: Literal["proof"] = "proof"
    daemon_nonce: Nonce
    mac: str

    def valid_for(self, challenge: Challenge, token: str) -> bool:
        """Whether this proof answers *challenge* for a daemon holding *token*."""
        expected = Handshake.daemon_mac(
            token, challenge.client_nonce, self.daemon_nonce
        )
        return Handshake.matches(self.mac, expected)
