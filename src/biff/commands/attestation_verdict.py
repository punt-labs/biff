"""What ``biff key show`` concluded about an agent's attestation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final


@final
@dataclass(frozen=True, slots=True)
class AttestationVerdict:
    """What ``biff key show`` concluded about an agent's attestation.

    ``state`` is what was actually checked:

    - ``valid`` and ``expired``: the attestation verifies against the owner key
      published under the owner's name AND that key matched the owner key
      pinned on this machine.  The pin is a user-writable file, so this is not
      proof against code running as the user.
    - ``unpinned`` and ``unpinned-expired``: the signature verifies against the
      key published under that name, but this machine holds no pin for the
      owner, so nothing local confirms that key.  The directory is a claim.
    - ``unverified``: the pin could not be read in this session, so nothing
      was checked.
    - ``invalid``: the pin contradicts the published key, the pin is damaged,
      or the attestation does not verify.
    - ``no-owner-key``: no key is published for the owner.
    """

    owner: str
    state: str
    standing: str
    text: str

    @classmethod
    def signed(
        cls,
        owner: str,
        who: str,
        standing: str,
        expiry: str,
        *,
        live: bool,
        pinned: bool,
    ) -> AttestationVerdict:
        """Judge a signature that verifies: live or expired, pinned or not."""
        outcome = f"valid until {expiry}" if live else f"expired {expiry}"
        if pinned:
            return cls(
                owner,
                "valid" if live else "expired",
                standing,
                f"authorized by {who}: {outcome}",
            )
        text = (
            f"authorized by {who}: signature verifies against the key published "
            f"under that name; nothing local confirms that key; {outcome}"
        )
        state = "unpinned" if live else "unpinned-expired"
        return cls(owner, state, standing, text)

    def as_json(self) -> dict[str, str]:
        """Return the verdict as ``biff --json key show`` reports it."""
        return {"owner": self.owner, "state": self.state, "standing": self.standing}
