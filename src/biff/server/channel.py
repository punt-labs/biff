"""Channel mode: the live stdio connection and its one send capability (DES-087)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping, MutableMapping
from types import TracebackType
from typing import Protocol, Self

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCNotification

from biff.talk_latch import LatchMessages, TalkNotifyLatch

logger = logging.getLogger(__name__)

CHANNEL_MODE_VARIABLE = "BIFF_CHANNEL_MODE"
CHANNEL_CAPABILITY = "claude/channel"
CHANNEL_METHOD = "notifications/claude/channel"

CHANNEL_SEND_TIMEOUT = 3.0


class ChannelUnavailableError(Exception):
    """No live client stream can carry the event."""


_SEND_FAILED = LatchMessages(
    onset="Tool list change not delivered; session kept, will retry",
    retry="Tool list change still not delivered; session kept",
    recovery="Tool list change delivered again",
)


def take_channel_mode(
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    """Read the channel-mode variable once and remove it from the environment.

    The Claude Code plugin sets it in the ``tty`` server's ``env``; no user
    does.  Removing it keeps child processes (``git``, a nested ``biff``) from
    inheriting a mode that belongs to this process alone.  Only ``1`` turns the
    mode on; any other value is logged, since a typo would otherwise be silent.
    """
    env = os.environ if environ is None else environ
    value = env.pop(CHANNEL_MODE_VARIABLE, None)
    if value not in (None, "1"):
        logger.warning(
            "%s=%r is not 1; channel mode stays off", CHANNEL_MODE_VARIABLE, value
        )
    return value == "1"


class _Sink(Protocol):
    """The two operations of a session-message write stream that biff uses."""

    async def send(self, item: SessionMessage, /) -> None:
        """Write one message."""

    async def aclose(self) -> None:
        """Close the stream."""


class ObservedWriteStream:
    """A write stream that remembers when a write failed.

    The SDK's dispatcher swallows ``BrokenResourceError`` and
    ``ClosedResourceError`` on every notification it sends, so nothing above it
    can tell a delivered event from a dropped one.  This sits underneath and
    records the failure before re-raising it for the dispatcher to swallow.
    """

    _inner: _Sink
    _is_open: bool

    def __new__(cls, inner: _Sink) -> Self:
        """Wrap *inner*, open until a write or close fails."""
        self = super().__new__(cls)
        self._inner = inner
        self._is_open = True
        return self

    @property
    def is_open(self) -> bool:
        """Whether every write so far reached the stream."""
        return self._is_open

    async def send(self, item: SessionMessage, /) -> None:
        """Write *item*, marking the stream dead if it is closed or broken."""
        try:
            await self._inner.send(item)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            self._is_open = False
            raise

    async def aclose(self) -> None:
        """Close the stream; later sends are refused."""
        self._is_open = False
        await self._inner.aclose()

    async def __aenter__(self) -> Self:
        """Enter the wrapped stream's context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close on exit, as the SDK's streams do."""
        await self.aclose()


class ChannelLink:
    """The server's live client stream, narrowed to sending channel events.

    ``serve_channel_stdio`` wraps the connection's write stream with
    :meth:`observe`, so a write the SDK drops marks the link closed.  A closed
    stream raises ``ChannelUnavailableError`` and a blocked write raises
    ``TimeoutError``.
    """

    _stream: ObservedWriteStream | None
    _failures: TalkNotifyLatch

    def __new__(cls) -> Self:
        """Create a link with no stream attached."""
        self = super().__new__(cls)
        self._stream = None
        self._failures = TalkNotifyLatch(logger, _SEND_FAILED)
        return self

    @property
    def is_open(self) -> bool:
        """Whether a stream is attached and nothing written to it has failed."""
        return self._stream is not None and self._stream.is_open

    def observe(self, write_stream: _Sink) -> ObservedWriteStream:
        """Attach *write_stream* and return it wrapped for the dispatcher."""
        self._stream = ObservedWriteStream(write_stream)
        return self._stream

    def detach(self) -> None:
        """Forget the stream once its connection has ended."""
        self._stream = None

    async def send(self, content: str, meta: Mapping[str, str]) -> None:
        """Send one ``notifications/claude/channel`` event.

        Raises:
            ChannelUnavailableError: No stream is attached, or it is closed.
            TimeoutError: The write did not complete within
                ``CHANNEL_SEND_TIMEOUT`` seconds.
        """
        stream = self._stream
        if stream is None or not stream.is_open:
            msg = "no live client connection"
            raise ChannelUnavailableError(msg)
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method=CHANNEL_METHOD,
            params={"content": content, "meta": dict(meta)},
        )
        try:
            async with asyncio.timeout(CHANNEL_SEND_TIMEOUT):
                await stream.send(SessionMessage(message=notification))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            msg = "the client connection is closed"
            raise ChannelUnavailableError(msg) from exc

    def require_open(self) -> None:
        """Raise ``ChannelUnavailableError`` if a dropped write closed the stream."""
        if not self.is_open:
            msg = "a write to the client was dropped"
            raise ChannelUnavailableError(msg)

    def delivered(self) -> None:
        """Record a successful send, logging recovery once after a failure."""
        self._failures.record_success()

    def failed(self, exc: Exception) -> None:
        """Log an expected send failure once per episode; re-raise any other.

        A code bug must not be absorbed into a retry loop.
        """
        if not isinstance(exc, (TimeoutError, ChannelUnavailableError)):
            raise exc
        self._failures.record_failure()
