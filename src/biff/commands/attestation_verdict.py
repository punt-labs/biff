"""What ``biff key show`` concluded about an agent's attestation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final


@final
@dataclass(frozen=True, slots=True)
class AttestationVerdict:
    """What ``biff key show`` concluded about an agent's attestation."""

    owner: str
    state: str
    standing: str
    text: str

    def as_json(self) -> dict[str, str]:
        """Return the verdict as ``biff --json key show`` reports it."""
        return {"owner": self.owner, "state": self.state, "standing": self.standing}
