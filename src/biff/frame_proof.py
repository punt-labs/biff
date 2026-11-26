"""The frames that prove the daemon holds the token before a hello is sent."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated, Literal

from pydantic import StringConstraints

from biff.frame_base import FrameBase

__all__ = ["Challenge", "Proof"]


class Challenge(FrameBase):
    """First frame of a connection: a random nonce the daemon must answer.

    It carries nothing secret.  A client sends its hello, which carries the
    bearer token, only after the daemon's :class:`Proof` of the token checks.
    """

    type: Literal["challenge"] = "challenge"
    nonce: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32,128}$")]

    @classmethod
    def fresh(cls) -> Challenge:
        """Return a challenge with a new 128-bit nonce."""
        return cls(nonce=secrets.token_hex(16))

    def answer(self, token: str) -> Proof:
        """Return the proof of holding *token* for this challenge."""
        return Proof(mac=Proof.mac_of(token, self.nonce))


class Proof(FrameBase):
    """The daemon's answer to a challenge: HMAC-SHA256 of the nonce under the token."""

    type: Literal["proof"] = "proof"
    mac: str

    @staticmethod
    def mac_of(token: str, nonce: str) -> str:
        """Return the hex HMAC-SHA256 of *nonce* keyed by *token*."""
        return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()

    def answers(self, challenge: Challenge, token: str) -> bool:
        """Whether this proof is the right answer to *challenge* under *token*."""
        expected = Proof.mac_of(token, challenge.nonce)
        return hmac.compare_digest(self.mac.encode(), expected.encode())
