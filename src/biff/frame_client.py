"""The frames a client sends: hello, request and subscribe."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from biff.frame_base import FrameBase
from biff.session_routing import MAX_ID_LENGTH

__all__ = ["ClientType", "Hello", "Request", "Subscribe"]

type ClientType = Literal["mcp", "cli", "hook", "plugin"]

# The charset a session id has everywhere else (``session_routing.ID_RE``);
# anchored because a pattern constraint searches rather than matches.
type _SessionId = Annotated[
    str, StringConstraints(pattern=rf"^[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}$")
]
type _Mac = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type _AgentId = Annotated[
    str, StringConstraints(pattern=rf"^[A-Za-z0-9_-]{{0,{MAX_ID_LENGTH}}}$")
]


class Hello(FrameBase):
    """Sent after the daemon's proof: a MAC that proves the token, and the caller.

    The MAC binds both handshake nonces, so it is good for this connection only;
    the token itself is never sent.
    """

    type: Literal["hello"] = "hello"
    mac: _Mac
    client: ClientType
    session_id: _SessionId
    # Empty when the harness states no main agent (a shell outside a harness).
    main_agent_id: _AgentId = ""
    # The directory the client runs in: it names the repository and relay.
    cwd: str
    # Overrides the configured identity, as ``--user`` does for the CLI.
    user: str = ""


class Request(FrameBase):
    """One command invocation: ``method`` names a command object."""

    type: Literal["request"] = "request"
    id: int = Field(ge=1)
    method: str
    params: dict[str, JsonValue] = Field(default_factory=dict)


class Subscribe(FrameBase):
    """Start the arrival subscription for the connection's session.

    The daemon answers with a reply to ``id``, so the client knows it started.
    """

    type: Literal["subscribe"] = "subscribe"
    id: int = Field(ge=1)
