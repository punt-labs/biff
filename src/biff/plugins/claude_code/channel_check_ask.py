"""How ``biff doctor`` asks biffd to run a channel check against one session."""

from __future__ import annotations

import asyncio
import math
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, final

from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.client.nudge import DaemonNudge
from biff.formatting import terminal_safe
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

__all__ = ["REQUEST_TIMEOUT_SECONDS", "ChannelAsk", "CheckNotSentError", "DaemonAsk"]

REQUEST_TIMEOUT_SECONDS = 10.0
"""What an ordinary request to biffd waits for a reply."""

_DAEMON_MARGIN_SECONDS = 15.0
"""How much longer than the wait the client waits: biffd answers after its wait."""


class CheckNotSentError(Exception):
    """The check was not sent; the message says why, in words fit for a line."""


class ChannelAsk(Protocol):
    """Asks one session to prove its channel events arrive."""

    async def confirmed(self, session_key: str, seconds: float) -> bool:
        """Whether *session_key* acknowledged the check within *seconds*.

        Raises:
            CheckNotSentError: The check could not be sent.
        """
        ...


@final
class DaemonAsk:
    """Asks biffd's ``channel_check`` method; biffd owns the relay connection.

    The doctor holds no relay for the check: the daemon sends the request,
    waits for the session's acknowledgement and answers whether it came.  The
    client's deadline is the wait plus a margin, because the daemon answers only
    after its own wait and the ordinary request timeout would end the call first.
    """

    __slots__ = ("_cwd", "_nudge", "_request_timeout", "_run_dir", "_user")

    _cwd: str
    _request_timeout: float
    _nudge: Callable[[], int]
    _run_dir: RunDir
    _user: str

    def __new__(
        cls,
        *,
        user: str,
        run_dir: RunDir,
        nudge: Callable[[], int],
        cwd: str,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Self:
        """Ask as *user* from *cwd*; *nudge* returns the port biffd answers on.

        *request_timeout* is the deadline of an ordinary request, which the check
        outlasts: the check's own deadline is longer.
        """
        self = super().__new__(cls)
        self._request_timeout = request_timeout
        self._user = user
        self._run_dir = run_dir
        self._nudge = nudge
        self._cwd = cwd
        return self

    @classmethod
    def for_this_process(cls, user: str | None) -> Self:
        """Return the ask at the default run directory; biffd starts if it is down."""
        run_dir = RunDir.default()

        def start_biffd() -> int:
            return DaemonNudge.for_host(run_dir).ensure_running()

        return cls(
            user=user or "",  # empty: the environment's user, as the CLI does
            run_dir=run_dir,
            nudge=start_biffd,
            cwd=str(Path.cwd().absolute()),
        )

    async def confirmed(self, session_key: str, seconds: float) -> bool:
        """Whether *session_key* acknowledged the check within *seconds*.

        Raises:
            CheckNotSentError: biffd is absent, cannot start, refused the request,
                or could not send the check (it needs the NATS relay).
        """
        try:
            return self._confirmed(await self._ask(session_key, seconds))
        except (OSError, TimeoutError, ValueError, DaemonError) as exc:
            raise CheckNotSentError(
                terminal_safe(str(exc)) or type(exc).__name__
            ) from exc

    async def _ask(self, session_key: str, seconds: float) -> JsonValue:
        port = await asyncio.to_thread(self._nudge)
        client = await DaemonClient.connect(
            run_dir=self._run_dir,
            port=port,
            request_timeout=self._request_timeout,
            identity=ClientIdentity(
                client="cli",
                session_id=str(uuid.uuid4()),
                cwd=self._cwd,
                user=self._user,
            ),
        )
        try:
            return await client.request_within(
                seconds + _DAEMON_MARGIN_SECONDS,
                "channel_check",
                to=session_key,
                seconds=math.ceil(seconds),  # the method takes whole seconds
            )
        finally:
            await client.close()

    @staticmethod
    def _confirmed(reply: JsonValue) -> bool:
        if not isinstance(reply, dict):
            msg = "biffd sent a reply this biff cannot read; run: biff daemon restart"
            raise CheckNotSentError(msg)
        data = reply.get("data")
        if reply.get("error") is True or not isinstance(data, dict):
            text = reply.get("text")
            raise CheckNotSentError(text if isinstance(text, str) else "check refused")
        return data.get("confirmed") is True
