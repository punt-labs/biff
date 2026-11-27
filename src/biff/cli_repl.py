"""The interactive ``biff`` prompt: commands, arrivals and talk, all through biffd."""

from __future__ import annotations

import logging
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, Self, final

from biff.cli_console import PromptedConsole, Tick
from biff.cli_daemon import DaemonCommand
from biff.cli_link import DaemonLink
from biff.cli_talk import TalkConversation
from biff.cli_talk_reading import TalkUnavailableError
from biff.cli_talk_view import TalkView
from biff.commands import CommandResult
from biff.dispatch import Call, available_commands, parse
from biff.models import WallPost
from biff.repl_display import ReplDisplay
from biff.repl_notify import NotifyState
from biff.repl_readline import setup as setup_readline

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import JsonValue

    from biff.cli_console import Console
    from biff.frames import Event

__all__ = ["ReplSession"]

_CLEAR_LINE = "\r\033[K"

logger = logging.getLogger(__name__)


@final
class ReplSession:
    """The REPL: reads command lines, runs them in biffd, shows arrivals.

    biffd holds the relay and the session, so this process holds neither: a
    command line is parsed here and run by name in biffd, and mail, walls and
    talk arrive as events on the standing connection.  A talk is a modal
    sub-loop (:class:`TalkConversation`) that borrows the same console.
    """

    __slots__ = (
        "_console",
        "_display",
        "_link",
        "_notify",
        "_prompt",
        "_unread",
        "_view",
        "_wall",
    )

    _link: DaemonLink
    _console: Console
    _prompt: str
    _display: ReplDisplay
    _view: TalkView
    _notify: NotifyState
    _unread: int
    _wall: WallPost | None

    def __new__(cls, *, link: DaemonLink, console: Console) -> Self:
        """Create a session over *link* that reads and prompts through *console*."""
        self = super().__new__(cls)
        self._link = link
        self._console = console
        self._prompt = console.prompt
        self._display = ReplDisplay()
        self._view = TalkView(self._display)
        self._notify = NotifyState()
        self._unread = 0
        self._wall = None
        return self

    @classmethod
    async def interactive(
        cls, command: DaemonCommand, read: Callable[[str], str] = input
    ) -> None:
        """Run the REPL on this terminal as *command*'s session.

        *read* reads one line for a prompt (``input``).

        Raises:
            ValueError: A named session cannot be used.
            ConnectionError: biffd is absent, cannot start, or lost the connection.
            DaemonError: biffd refused the connection or a request.
        """
        commands = available_commands()
        setup_readline(commands)
        link = await DaemonLink.open(command)
        try:
            status = await link.status()
            prompt = f"{status['user']}:{status['tty_name']} ▶ "
            console = PromptedConsole(prompt, read)
            session = cls(link=link, console=console)
            console.say(
                f"biff {pkg_version('punt-biff')} — {prompt.removesuffix(' ▶ ')}"
            )
            console.say(f"Commands: {', '.join(commands)}, talk, timestamps, exit")
            console.say()
            await session.collect_held_talk()
            console.start()
            try:
                await session.run(status)
            finally:
                await console.stop()
        finally:
            await link.close()

    async def collect_held_talk(self) -> None:
        """Show talk biffd held before this REPL attached; notices are not replayed."""
        try:
            banners = await self._talk_banners()
        except TalkUnavailableError:
            return  # this relay carries no talk, so nothing can be held
        for banner in banners:
            self._console.say(banner)

    async def run(self, status: dict[str, JsonValue]) -> None:
        """Read and run lines until ``exit``, end of input, or a lost connection.

        *status* is the session's ``status`` data, the baseline arrivals are
        measured from.
        """
        self._baseline(status)
        wake = self._link.pump.wake
        while True:
            got = await self._console.next(wake)
            if self._link.pump.lost:
                lost = self._link.pump.lost
                self._console.say(f"{_CLEAR_LINE}biffd connection lost: {lost}")
                return
            if got is Tick.IDLE:
                wake.clear()
                await self._guarded(self._arrivals())
                continue
            if got is None:
                self._console.say()
                return
            if not await self._line(got):
                return
            self._console.release()

    async def _line(self, line: str) -> bool:
        """Run one typed line; return ``False`` when the REPL should end."""
        tokens = line.split(None, 2)
        if tokens and tokens[0].lower() == "talk":
            await self._guarded(self._talk(tokens[1:]))
            return True
        if tokens and tokens[0].lower() == "timestamps":
            self._timestamps(tokens[1:])
            return True
        parsed = parse(line)
        if parsed is None:
            return False
        if isinstance(parsed, CommandResult):
            if parsed.text:
                self._console.say(parsed.text)
            return True
        await self._guarded(self._command(parsed))
        return True

    async def _command(self, call: Call) -> None:
        """Run *call* in biffd, show its result, and take it as seen."""
        await self._link.run(call, lambda result: self._console.say(result.text))
        # The user's own command moved the state: measure arrivals from here, so
        # a wall just posted is not announced back to its author.
        self._baseline(await self._link.status())

    async def _talk(self, args: list[str]) -> None:
        if not args:
            self._console.say("Usage: talk user:ttyN [message]")
            return
        conversation = TalkConversation(
            link=self._link, console=self._console, view=self._view, prompt=self._prompt
        )
        await conversation.run(args[0], " ".join(args[1:]))

    async def _guarded(self, work: Awaitable[None]) -> None:
        """Run *work*; a refusal or an absent biffd is a message, not the end."""
        try:
            await work
        except DaemonCommand.FAILURES as exc:
            self._console.complain(f"Error: {exc}")

    def _timestamps(self, args: list[str]) -> None:
        """Handle the REPL-only ``timestamps on|off`` toggle."""
        if len(args) != 1 or args[0].lower() not in ("on", "off"):
            self._console.say("Usage: timestamps on|off")
            return
        on = args[0].lower() == "on"
        self._display.set_timestamps(on=on)
        self._console.say(f"Timestamps {'on' if on else 'off'}.")

    def _baseline(self, status: dict[str, JsonValue]) -> None:
        unread, wall = status.get("unread"), status.get("wall")
        self._unread = unread if isinstance(unread, int) else 0
        self._wall = WallPost.model_validate(wall) if isinstance(wall, dict) else None
        self._notify.sync(self._unread, self._wall)

    async def _arrivals(self) -> None:
        """Print what arrived since the last tick, above a fresh prompt."""
        notes: list[str] = []
        talked = False
        for event in self._link.pump.take():
            if event.kind.startswith("talk_"):
                talked = True
            else:
                notes.extend(self._note(event))
        if talked:
            try:
                notes.extend(await self._talk_banners())
            except TalkUnavailableError:
                logger.debug("talk notice on a relay that carries no talk")
        await self._show(notes)

    def _note(self, event: Event) -> list[str]:
        """Return the banners a mail or wall event calls for."""
        if event.kind == "wall" and isinstance(event.data, dict):
            self._wall = WallPost.model_validate(event.data)
        elif event.kind in ("unread", "mail") and isinstance(event.data, dict):
            count = event.data.get("count")
            self._unread = count if isinstance(count, int) else self._unread
        else:
            return []
        return self._notify.check(self._unread, self._wall)

    async def _talk_banners(self) -> list[str]:
        return self._view.banners(await self._link.talk_read())

    async def _show(self, notes: list[str]) -> None:
        """Print *notes* over the prompt line, then show the prompt again."""
        if not notes:
            return
        say = self._console.say
        say(_CLEAR_LINE, end="")
        for note in notes:
            say(note)
        say(self._console.prompt, end="")
