"""The harness-declared caller of a tool call: ``{session_id, agent_id}``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from biff.session_routing import ID_RE, SessionRouting

CALLER_KEY = "_caller"


class CallerIdentityError(ValueError):
    """The call carried no usable ``_caller``."""


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """Who is calling, as stated by the harness and checked for shape.

    The shape check is a well-formedness gate: the value is used as a path
    component and a hash input.  It routes; it does not authenticate.
    """

    session_id: str
    agent_id: str

    @classmethod
    def from_wire(cls, raw: object) -> CallerIdentity:
        """Parse the ``_caller`` argument, raising on any malformed value."""
        if not isinstance(raw, dict):
            msg = f"{CALLER_KEY} must be an object with session_id and agent_id"
            raise CallerIdentityError(msg)
        mapping = cast("dict[str, object]", raw)
        fields: dict[str, str] = {}
        for name in ("session_id", "agent_id"):
            value = mapping.get(name)
            if not isinstance(value, str) or not ID_RE.fullmatch(value):
                msg = (
                    f"{CALLER_KEY}.{name} must match [A-Za-z0-9_-]{{1,128}}, "
                    f"got {value!r}"
                )
                raise CallerIdentityError(msg)
            fields[name] = value
        return cls(session_id=fields["session_id"], agent_id=fields["agent_id"])

    @property
    def routing(self) -> SessionRouting:
        """Routing derivations for this caller's session."""
        return SessionRouting(self.session_id)

    @property
    def routing_id(self) -> str:
        """Routing token for this caller's session."""
        return self.routing.routing_id
