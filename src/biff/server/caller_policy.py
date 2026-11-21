"""Fixed process callers and strict per-call identity validation."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from uuid import uuid4

import typer
from fastmcp.exceptions import ToolError

from biff.caller_identity import CALLER_KEY, CallerIdentity, CallerIdentityError
from biff.session_routing import ID_RE

type CallerBinder = Callable[[CallerIdentity], AbstractAsyncContextManager[None]]

_logger = logging.getLogger(__name__)
_UNIDENTIFIED = (
    f"call carried no {CALLER_KEY}: the biff stamping hook is not installed, "
    "is disabled (.punt-hooks-kill), or was killed before this call. "
    "Run `biff doctor`, then restart the session."
)


@dataclass(frozen=True)
class CallerPolicy:
    """Resolve stamps and start presence through the same session binding."""

    _bind: CallerBinder
    _default: CallerIdentity | None = None

    @staticmethod
    def for_launch(user: str | None, transport: str) -> CallerIdentity | None:
        """Validate a fixed caller only for an explicit single-user stdio launch."""
        if user is None or transport != "stdio":
            return None
        try:
            return CallerIdentity.from_wire(
                {
                    "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID")
                    or str(uuid4()),
                    "agent_id": user,
                }
            )
        except CallerIdentityError as exc:
            raise typer.BadParameter(
                "Explicit --user requires a valid CLAUDE_CODE_SESSION_ID and "
                "user handle matching [A-Za-z0-9_-]{1,128}"
            ) from exc

    def resolve(self, arguments: dict[str, object] | None) -> CallerIdentity:
        """Pop a valid stamp, falling back only when no stamp was supplied."""
        if arguments is None or CALLER_KEY not in arguments:
            return self._unstamped()
        try:
            return CallerIdentity.from_wire(arguments.pop(CALLER_KEY))
        except CallerIdentityError as exc:
            _logger.warning("Tool call denied: malformed caller identity")
            raise ToolError(str(exc)) from exc

    def _unstamped(self) -> CallerIdentity:
        if self._default is not None:
            return self._default
        _logger.warning("Tool call denied: missing caller identity")
        raise ToolError(_UNIDENTIFIED)

    def bind(self, caller: CallerIdentity) -> AbstractAsyncContextManager[None]:
        """Keep the session binding owned for the duration of a call."""
        return self._bind(caller)

    def startup(self) -> AbstractAsyncContextManager[None]:
        """Capture startup presence without treating the environment as a caller."""
        caller = self._default
        if caller is None:
            session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
            if not ID_RE.fullmatch(session):
                return nullcontext()
            caller = CallerIdentity(session, "main")
        return self.bind(caller)
