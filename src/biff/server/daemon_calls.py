"""What every biffd-backed tool does to reach the daemon: one session, one reply."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Self, final

from fastmcp.exceptions import ToolError

from biff.client.connection import DaemonError
from biff.frame_reply import ErrorCode
from biff.server.mcp_link import FAILURES

if TYPE_CHECKING:
    from pydantic import JsonValue

    from biff.server.mcp_link import Action, McpLink, Reply
    from biff.server.state import ServerState

__all__ = ["DaemonCalls"]

_NO_SESSION: Final = (
    "this call named no session; run biff doctor, then restart the session"
)
_HELD_ELSEWHERE: Final = (
    "; biffd keeps the session on the relay it first used until it restarts: "
    "run biff daemon restart"
)


@final
class DaemonCalls:
    """Asks biffd for the session a tool call belongs to, failing as a tool error.

    A biffd that is not installed, did not start, or refused the request is a
    ``ToolError`` whose message names the cause and the command that repairs it,
    so the model and the person see the same thing.
    """

    __slots__ = ("_link", "_state")

    _state: ServerState
    _link: McpLink

    def __new__(cls, state: ServerState, link: McpLink) -> Self:
        """Ask for the sessions of *state* through *link*."""
        self = super().__new__(cls)
        self._state = state
        self._link = link
        return self

    async def ask(self, method: str, **params: JsonValue) -> Reply:
        """Run *method* for the current session and return its reply.

        Raises:
            ToolError: biffd cannot be reached or refused the request.
        """
        try:
            return await self._link.ask(self.session(), method, **params)
        except (*FAILURES, DaemonError) as exc:
            raise ToolError(self._said(exc)) from exc

    async def read(self, then: Action) -> Reply:
        """Read the current session's mail; *then* runs once it is confirmed.

        Raises:
            ToolError: biffd cannot be reached or refused the request.
        """
        try:
            return await self._link.read(self.session(), then)
        except (*FAILURES, DaemonError) as exc:
            raise ToolError(self._said(exc)) from exc

    def session(self) -> str:
        """Return the session id the current call is bound to.

        Raises:
            ToolError: The call named no session.
        """
        session = self._state.bound_session_id
        if session is None:
            raise ToolError(_NO_SESSION)
        return session

    @staticmethod
    def _said(exc: Exception) -> str:
        """Return what the model and the person are told when biffd refuses."""
        if isinstance(exc, DaemonError) and exc.code is ErrorCode.SESSION_CONFLICT:
            return f"{exc}{_HELD_ELSEWHERE}"
        return str(exc)
