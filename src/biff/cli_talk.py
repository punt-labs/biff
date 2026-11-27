"""One talk as a person types it: invite, wait, converse, hang up."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from biff._formatting import terminal_safe
from biff.cli_console import PromptedConsole, Tick
from biff.cli_link import DaemonLink
from biff.cli_talk_view import TalkView
from biff.client.connection import DaemonError
from biff.repl_display import ReplDisplay

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.cli_console import Console
    from biff.cli_daemon import DaemonCommand
    from biff.talk_reading import TalkReading
    from biff.talk_wire import WireMessage

__all__ = ["TalkConversation"]

logger = logging.getLogger(__name__)

_CLEAR_LINE = "\r\033[K"
_LEAVE_WORDS = frozenset({"end", "exit", "quit"})


@final
class TalkConversation:
    """Drives one talk through biffd: the invite, the wait, the lines, the hangup.

    biffd holds the conversation; this is its front-end.  A notice wakes the
    loop, and the loop pulls the text with ``talk_read``, so a line costs one
    round trip after it arrives.  The REPL runs it from inside its own loop and
    ``biff talk`` runs it alone; both hand it the console they read from.
    """

    __slots__ = ("_carried", "_console", "_link", "_lost", "_prompt", "_view")

    _link: DaemonLink
    _console: Console
    _view: TalkView
    _prompt: str
    _carried: tuple[WireMessage, ...]
    _lost: bool

    def __new__(
        cls, *, link: DaemonLink, console: Console, view: TalkView, prompt: str
    ) -> Self:
        """Create a conversation that reads *console*, prompting *prompt* in talk."""
        self = super().__new__(cls)
        self._link = link
        self._console = console
        self._view = view
        self._prompt = prompt
        self._carried = ()
        self._lost = False
        return self

    @classmethod
    async def alone(
        cls,
        command: DaemonCommand,
        to: str,
        opening: str,
        read: Callable[[str], str] = input,
    ) -> bool:
        """Run ``biff talk``: open a link, converse with *to*, close the link.

        *read* reads one line for a prompt (``input``).  Returns ``False`` when
        biffd refused the talk, for instance because the relay carries none.

        Raises:
            ValueError: A named session cannot be used.
            ConnectionError: biffd is absent, cannot start, or lost the connection.
            DaemonError: biffd refused the connection or a request.
        """
        link = await DaemonLink.open(command)
        console = PromptedConsole("you> ", read)
        view = TalkView(ReplDisplay())
        talk = cls(link=link, console=console, view=view, prompt="you> ")
        console.start()
        try:
            return await talk.run(to, opening)
        finally:
            await console.stop()
            await link.close()

    async def run(self, to: str, opening: str) -> bool:
        """Talk to *to*, starting with *opening* when it is not empty.

        An address that names someone with an invite to us accepts it; one that
        names the peer of a live talk sends *opening* as a line; any other
        invites.  Returns when the conversation is over: ``False`` if biffd
        refused it or the connection to biffd was lost, ``True`` however else it
        ended.
        """
        say = self._console.say
        outcome = await self._link.talk(to, opening)
        if outcome.error or outcome.phase == "idle":
            say(terminal_safe(outcome.text))
            return not outcome.error
        # The peer's name came off the network: the daemon cleans it, and so
        # does every place it is shown or published.
        peer = terminal_safe(outcome.peer)
        await self._link.set_plan(f"talking to {peer}")
        try:
            if outcome.phase == "inviting" and not await self._await_accept(peer):
                return not self._lost
            await self._converse(peer)
        finally:
            await self._link.set_plan("")
        return not self._lost

    async def _await_accept(self, peer: str) -> bool:
        """Wait for *peer* to accept; return whether the talk connected.

        Typing ``end`` cancels and withdraws the invite.  A Ctrl-C withdraws it
        too and is re-raised, so the process still exits: the invitee's
        ``[TALK]`` marker clears at once rather than when the invite expires.
        """
        say, wake = self._console.say, self._link.pump.wake
        say(_CLEAR_LINE, end="")
        say(f"Waiting for {peer} to respond... (type 'end' to cancel)")
        self._console.release()
        try:
            while True:
                got = await self._console.next(wake)
                if self._link.pump.lost:
                    say(terminal_safe(f"biffd connection lost: {self._link.pump.lost}"))
                    self._lost = True
                    return False
                if got is Tick.IDLE:
                    wake.clear()
                    if await self._accepted(peer):
                        return True
                elif not isinstance(got, str) or got.strip().lower() in _LEAVE_WORDS:
                    say(f"Talk with {peer} cancelled.")
                    await self._withdraw()
                    return False
                else:
                    self._console.release()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self._withdraw()
            raise

    async def _accepted(self, peer: str) -> bool:
        """Read what arrived while waiting; return whether *peer* accepted."""
        say = self._console.say
        self._link.pump.discard_talk()
        reading = await self._link.talk_read()
        for banner in self._view.invites(reading):
            say(f"{_CLEAR_LINE}{banner}")
        # The accepter's opening line renders once Connected prints.
        self._carried = (*self._carried, *reading.messages)
        if reading.phase != "connected":
            return False
        if reading.opening:
            say(f"you> {terminal_safe(reading.opening)}")
        if reading.opening_unsent:
            say("Warning: your opening line was not sent; it goes with the next read.")
        if not reading.accept_published:
            say(
                f"Warning: couldn't confirm {peer} joined — they may not have "
                "connected. Send a message or type 'end' and retry."
            )
        return True

    async def _converse(self, peer: str) -> None:
        """Show what the peer says and send what is typed, until either hangs up."""
        console, wake = self._console, self._link.pump.wake
        resting = console.prompt
        console.set_prompt(self._prompt)
        console.say(f"Connected to {peer}. Type 'end' to return to REPL.\n")
        console.release()
        # The accepter's opening line is already held: wake the first tick so it
        # renders through the same path as every later line.
        wake.set()
        over = False
        try:
            while not over:
                got = await console.next(wake)
                if self._link.pump.lost:
                    lost = terminal_safe(self._link.pump.lost)
                    console.say(f"{_CLEAR_LINE}biffd connection lost: {lost}")
                    self._lost = True
                    over = True
                elif got is Tick.IDLE:
                    wake.clear()
                    over = (await self._show_arrivals()).ended
                elif not isinstance(got, str) or got.strip().lower() == "end":
                    break
                else:
                    await self._send(peer, got.strip())
                    console.release()
        finally:
            console.set_prompt(resting)
            if not over:
                await self._withdraw()
        console.say(f"{_CLEAR_LINE}Talk with {peer} ended.")

    async def _show_arrivals(self) -> TalkReading:
        """Print what the peer sent, above a fresh prompt; return what was read."""
        self._link.pump.discard_talk()
        reading = await self._link.talk_read()
        shown = [
            *self._view.dropped(reading),
            *self._view.conversation((*self._carried, *reading.messages)),
        ]
        self._carried = ()
        if shown:
            say = self._console.say
            say(_CLEAR_LINE, end="")
            for line in shown:
                say(line)
            if not reading.ended:
                say(self._console.prompt, end="")
        return reading

    async def _send(self, peer: str, line: str) -> None:
        """Send *line* to *peer*; an empty line sends nothing."""
        if not line:
            return
        sent = await self._link.talk(peer, line)
        if sent.error:
            self._console.say(f"{_CLEAR_LINE}{terminal_safe(sent.text)}")

    async def _withdraw(self) -> None:
        """End the talk, or withdraw the invite; never raise out of a hangup.

        biffd resets its side whatever the relay does, so a failure here only
        means the peer may not have been told: biffd's text says which, and is
        shown.  A connection that is already gone is logged.
        """
        try:
            ended = await self._link.talk_end()
        except (OSError, TimeoutError, DaemonError):
            logger.warning("could not tell biffd the talk ended", exc_info=True)
            return
        if ended.error:
            self._console.say(f"{_CLEAR_LINE}{terminal_safe(ended.text)}")
