"""Channel mode: the live stdio connection and its one send capability (DES-087)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Mapping, MutableMapping
from types import TracebackType
from typing import Protocol, Self

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCNotification

from biff.failure_log import TRANSPORT_ERRORS, EpisodeLog

logger = logging.getLogger(__name__)

CHANNEL_MODE_VARIABLE = "BIFF_CHANNEL_MODE"
CHANNEL_CAPABILITY = "claude/channel"
CHANNEL_METHOD = "notifications/claude/channel"

CHANNEL_SEND_TIMEOUT = 3.0
CHANNEL_BACKOFF = 30.0
HANDSHAKE_WINDOW = 30.0


class ChannelUnavailableError(Exception):
    """No live client stream can carry the event."""


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


# Failures of a client that stopped listening; anything else is a bug.
_EXPECTED = (ChannelUnavailableError, *TRANSPORT_ERRORS)


class _Sink(Protocol):
    """The two operations of a session-message write stream that biff uses."""

    async def send(self, item: SessionMessage, /) -> None:
        """Write one message."""

    async def aclose(self) -> None:
        """Close the stream."""


class _Source(Protocol):
    """The read operations the dispatcher uses on a session-message stream."""

    async def __anext__(self) -> SessionMessage | Exception:
        """Return the next message."""
        ...

    async def receive(self) -> SessionMessage | Exception:
        """Return the next message, or raise at the end of the stream."""
        ...

    async def aclose(self) -> None:
        """Close the stream."""


class ObservedReadStream:
    """A read stream that reports when the client's input ends.

    After end of input the SDK's dispatcher stops writing but keeps running
    until its handlers finish, so a link that waits for ``run`` to return would
    report open while every write is dropped.
    """

    _inner: _Source
    _on_end: Callable[[], None]

    def __new__(cls, inner: _Source, on_end: Callable[[], None]) -> Self:
        """Wrap *inner*, calling *on_end* once when it ends."""
        self = super().__new__(cls)
        self._inner = inner
        self._on_end = on_end
        return self

    @property
    def last_context(self) -> object:
        """The sender's context for the last message, which the SDK restores."""
        return getattr(self._inner, "last_context", None)

    def __aiter__(self) -> Self:
        """Iterate messages until the input ends."""
        return self

    async def __anext__(self) -> SessionMessage | Exception:
        """Return the next message, reporting the end of input."""
        try:
            return await self._inner.__anext__()
        except (StopAsyncIteration, anyio.ClosedResourceError):
            self._on_end()
            raise StopAsyncIteration from None

    async def receive(self) -> SessionMessage | Exception:
        """Return the next message, reporting the end of input."""
        try:
            return await self._inner.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            self._on_end()
            raise

    async def aclose(self) -> None:
        """Close the stream."""
        self._on_end()
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
    :meth:`observe` and the read stream with :meth:`observe_reads`, so a write
    the SDK drops, or the end of the client's input, marks the link closed.  A
    closed stream raises ``ChannelUnavailableError`` and a blocked write raises
    ``TimeoutError``.
    """

    _stream: ObservedWriteStream | None = None
    _log: EpisodeLog
    _retry_after: float = 0.0
    _created_at: float

    def __new__(cls) -> Self:
        """Create a link with no stream attached."""
        self = super().__new__(cls)
        self._log = EpisodeLog(logger, _EXPECTED)
        self._created_at = time.monotonic()
        return self

    @property
    def is_open(self) -> bool:
        """Whether a stream is attached and nothing written to it has failed."""
        return self._stream is not None and self._stream.is_open

    def observe(self, write_stream: _Sink) -> ObservedWriteStream:
        """Attach *write_stream* and return it wrapped for the dispatcher."""
        self._stream = ObservedWriteStream(write_stream)
        return self._stream

    def observe_reads(self, read_stream: _Source) -> ObservedReadStream:
        """Return *read_stream* wrapped so that its end closes the link."""
        return ObservedReadStream(read_stream, self.detach)

    def detach(self) -> None:
        """Forget the stream once its connection has ended."""
        self._stream = None

    async def send(self, content: str, meta: Mapping[str, str]) -> None:
        """Send one ``notifications/claude/channel`` event.

        A normal return means the event was handed to the stdout writer task,
        not that the client read it: the stdio write stream has no buffer, so a
        later ``BrokenPipeError`` surfaces in that task, not here.

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

    def window_remaining(self) -> float:
        """Seconds until the server may expect ``initialize``; 0 once it may.

        Measured from when the link was created, which is before any startup
        notification can run, attached stream or not.
        """
        return max(self._created_at + HANDSHAKE_WINDOW - time.monotonic(), 0.0)

    def ready(self) -> bool:
        """Whether a retry that nothing asked for may run now (after the back-off)."""
        return time.monotonic() >= self._retry_after

    def seconds_until_ready(self) -> float:
        """Seconds until :meth:`ready`; 0 once it is."""
        return max(self._retry_after - time.monotonic(), 0.0)

    def delivered(self, what: str) -> None:
        """Record that *what* was sent, logging recovery once after a failure."""
        self._retry_after = 0.0
        self._log.recovered(what)

    def failed(self, what: str, exc: Exception) -> None:
        """Log that *what* failed, once per episode, and start the back-off."""
        self._retry_after = time.monotonic() + CHANNEL_BACKOFF
        self._log.failed(what, exc)
