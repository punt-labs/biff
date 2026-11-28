"""Mail a stopping daemon could not give back, kept on disk for the next start."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final, Self, final

from pydantic import BaseModel, ConfigDict, ValidationError

from biff.daemon.durable_file import DurableFile
from biff.daemon.resolver import ResolveError
from biff.frame_client import Hello
from biff.models import Message
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from biff.daemon.connection import DaemonCore
    from biff.daemon.sessions import Session
    from biff.frame_client import ClientType

__all__ = ["Spill"]

logger = logging.getLogger(__name__)

_NO_MAC: Final = "0" * 64
"""A spilled session has no connection, so no proof; the field only has a shape."""

type _Key = tuple[str, bool]
"""A session's id, and whether it is an agent's: the two are different inboxes."""


class _Spilled(BaseModel):
    """The mail one session still had unfiled, and what finds its relay again."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    agent: bool
    cwd: str
    user: str
    messages: tuple[Message, ...]

    @property
    def key(self) -> _Key:
        """The session this mail was held for."""
        return (self.session_id, self.agent)

    def hello(self) -> Hello:
        """Return the hello that resolves to this session's relay."""
        client: ClientType = "mcp" if self.agent else "cli"
        return Hello(
            mac=_NO_MAC,
            client=client,
            session_id=self.session_id,
            cwd=self.cwd,
            user=self.user,
        )

    def with_mail(self, messages: Sequence[Message]) -> Self:
        """Return this with *messages* added; one already here is not twice."""
        merged = {m.id: m for m in (*self.messages, *messages)}
        return self.model_copy(update={"messages": tuple(merged.values())})


class _SpillFile(BaseModel):
    """What the file holds: every session's unfiled mail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spilled: tuple[_Spilled, ...] = ()


@final
class Spill:
    """One 0600 file of the mail a stopping daemon could not file back.

    A consuming relay removed that mail at fetch, so while it is only in memory a
    stop that cannot file it in time (the budget ran out, the relay refused, a
    fetch finished after the hand-back began) would lose it.  The next start
    files it back before it serves.
    """

    __slots__ = ("_path", "_pending")

    _path: Path
    _pending: dict[_Key, _Spilled] | None

    def __new__(cls, path: Path) -> Self:
        """Keep the spill in the file *path*, created with the first message."""
        self = super().__new__(cls)
        self._path = path
        self._pending = None
        return self

    def hold(self, session: Session, messages: Sequence[Message]) -> None:
        """Keep *messages*, held for *session*, on disk."""
        if not messages:
            return
        pending = self._load()
        key = (session.session_id, session.is_agent)
        spilled = pending.get(key) or _Spilled(
            session_id=session.session_id,
            agent=session.is_agent,
            cwd=session.cwd,
            user=session.context.user,
            messages=(),
        )
        pending[key] = spilled.with_mail(messages)
        try:
            self._write(pending)
        except OSError:
            ids = ", ".join(str(m.id) for m in messages)
            logger.exception("could not spill messages %s of session %s", ids, key[0])

    async def refile(self, core: DaemonCore) -> None:
        """File every spilled message back to its relay, then forget it.

        What cannot be filed now (the relay is down, the repository is gone) stays
        in the file for the next start; refiled copies carry derived ids, so a
        message filed twice is one.
        """
        spilled = tuple(self._load().values())
        left = [
            rest
            for item in spilled
            if (rest := await self._file(core, item)) is not None
        ]
        self._pending = {rest.key: rest for rest in left}
        self._write(self._pending)

    @staticmethod
    async def _file(core: DaemonCore, item: _Spilled) -> _Spilled | None:
        """File *item*'s mail back; return what could not be (``None``: all of it)."""
        filed = 0
        try:
            async with asyncio.timeout(core.attach_timeout):
                resolved = await core.resolver.resolve(item.hello())
                relay = core.pool.acquire(resolved)
                for message in item.messages:
                    await relay.deliver(message.refiled())
                    filed += 1
        except (ResolveError, TimeoutError, *RELAY_ERRORS):
            logger.exception(
                "could not file spilled mail of session %s", item.session_id
            )
            return item.model_copy(update={"messages": item.messages[filed:]})
        return None

    def _load(self) -> dict[_Key, _Spilled]:
        """Return what the file holds, read once; an unreadable file is set aside."""
        if self._pending is None:
            self._pending = {}
            try:
                text = self._path.read_text()
            except FileNotFoundError:
                return self._pending
            try:
                items = _SpillFile.model_validate_json(text).spilled
            except ValidationError:
                aside = self._path.with_suffix(".corrupt")
                logger.exception("unreadable spill moved to %s", aside)
                self._path.replace(aside)
            else:
                self._pending = {item.key: item for item in items}
        return self._pending

    def _write(self, pending: dict[_Key, _Spilled]) -> None:
        """Replace the file with *pending*; no mail left, no file."""
        kept = tuple(item for item in pending.values() if item.messages)
        if not kept:
            self._path.unlink(missing_ok=True)
            return
        text = _SpillFile(spilled=kept).model_dump_json(indent=2)
        DurableFile.replace(self._path.parent, self._path.name, text)
