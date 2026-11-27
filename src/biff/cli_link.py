"""An interactive command's standing connection to biffd, and what it asks over it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from biff.cli_daemon_wire import WireResult
from biff.cli_events import EventPump
from biff.cli_talk_wire import TalkOutcome, TalkReading
from biff.client.connection import DaemonError
from biff.commands import CommandResult
from biff.dispatch import Call

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

    from biff.cli_daemon import DaemonCommand
    from biff.client.connection import DaemonClient

__all__ = ["DaemonLink"]

logger = logging.getLogger(__name__)


@final
class DaemonLink:
    """One subscribed connection that a REPL or a talk holds for its whole life.

    Talk requests and the subscription ride this connection, so a notice and the
    request it prompts share a session.  A product command (``who``, ``write``)
    opens its own connection through the :class:`DaemonCommand`, exactly as a
    one-shot ``biff`` command does, so it behaves the same in both.
    """

    __slots__ = ("_client", "_command", "_pump")

    _command: DaemonCommand
    _client: DaemonClient
    _pump: EventPump

    def __new__(cls, command: DaemonCommand, client: DaemonClient) -> Self:
        """Wrap *client*, a subscribed connection opened through *command*."""
        self = super().__new__(cls)
        self._command = command
        self._client = client
        self._pump = EventPump(client)
        return self

    @classmethod
    async def open(cls, command: DaemonCommand) -> Self:
        """Connect as *command*'s session, subscribe, and start reading events.

        Raises:
            ValueError: A named session cannot be used.
            ConnectionError: biffd is absent, cannot start, or lost the connection.
            DaemonError: biffd refused the connection or the subscription.
        """
        client, _ = await command.open()
        try:
            await client.subscribe()
        except BaseException:
            await client.close()
            raise
        link = cls(command, client)
        link._pump.start()
        return link

    @property
    def pump(self) -> EventPump:
        """The events biffd pushes to this session."""
        return self._pump

    async def close(self) -> None:
        """Stop reading and close the connection; the session stays in biffd."""
        await self._pump.stop()
        await self._client.close()

    async def run(self, call: Call, show: Callable[[CommandResult], None]) -> bool:
        """Run a product command as this session, show its result, and confirm it.

        Returns whether the command reported an error.

        Raises:
            ValueError: A named session cannot be used.
            ConnectionError: biffd is absent or sent an unreadable reply.
            DaemonError: biffd refused the request.
        """
        if call.method == "plan":
            answer = await self._command.plan(str(call.params["message"]))
        elif call.method == "tty":
            answer = await self._command.tty(str(call.params["name"]))
        else:
            answer = await self._command.call(call.method, **call.params)
        await answer.deliver(show)
        return answer.result.error

    async def status(self) -> dict[str, JsonValue]:
        """Return the session's ``status`` data: user, tty name, unread, wall."""
        data = (await self._ask("status")).data
        return data if isinstance(data, dict) else {}

    async def talk(self, to: str, message: str) -> TalkOutcome:
        """Invite *to*, accept their invite, or send *message* in a live talk."""
        return TalkOutcome.of(await self._ask("talk", to=to, message=message))

    async def talk_read(self) -> TalkReading:
        """Drain what the session's talk state holds.

        Raises:
            TalkUnavailableError: The session's relay carries no talk.
        """
        return TalkReading.of(await self._ask("talk_read"))

    async def talk_end(self) -> CommandResult:
        """End the talk, or withdraw the invite."""
        return (await self._ask("talk_end")).command_result()

    async def set_plan(self, text: str) -> None:
        """Set the plan shown in ``who``; a failure is logged, not raised.

        A cosmetic presence line must never end a conversation.
        """
        try:
            await self._ask("plan", message=text)
        except (OSError, TimeoutError, ValueError, DaemonError):
            logger.debug("could not set the talk plan", exc_info=True)

    async def _ask(self, method: str, **params: JsonValue) -> WireResult:
        return WireResult.parse(await self._client.request(method, **params))
