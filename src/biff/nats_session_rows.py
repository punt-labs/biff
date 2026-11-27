"""Compare-and-set edits of presence rows in the NATS key-value bucket."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nats.js.errors import (
    BucketNotFoundError,
    KeyNotFoundError,
    KeyWrongLastSequenceError,
)

from biff.models import UserSession
from biff.session_edit import SessionChangedError

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from nats.js import JetStreamContext
    from nats.js.kv import KeyValue

    from biff.session_edit import SessionEdit

__all__ = ["SessionRows"]

_TRIES = 3  # a later heartbeat tick retries what these do not land


class SessionRows:
    """The compare-and-set a NATS relay applies to a session row.

    A mixin of the relay: it needs the relay's connection, its request
    accounting and its row key, declared below and supplied by the relay.
    """

    __slots__ = ()

    def _kv_key(self, session_key: str) -> str:
        raise NotImplementedError

    async def _ensure_connected(self) -> tuple[JetStreamContext, KeyValue]:
        raise NotImplementedError

    async def _tracked[T](self, operation: str, awaitable: Awaitable[T]) -> T:
        raise NotImplementedError

    async def update_session_if(self, edit: SessionEdit) -> UserSession | None:
        """Store ``edit.change(row)`` if no one wrote the row since it was read.

        ``kv.update`` names the revision that was read and fails if the row has
        moved; the read, the change and the write are then tried again, at most
        three times, so a plan saved in between is seen by the change and never
        overwritten.  ``None`` means the row is absent.

        Raises:
            SessionChangedError: Other writers won every try.
        """
        kv_key = self._kv_key(edit.key)
        for _ in range(_TRIES):
            _, kv = await self._ensure_connected()
            try:
                entry = await self._tracked("kv.get", kv.get(kv_key))
            except (KeyNotFoundError, BucketNotFoundError):
                return None
            if entry.value is None:
                return None
            existing = UserSession.model_validate_json(entry.value)
            updated = edit.change(existing)
            if updated is existing:
                return existing
            _, kv = await self._ensure_connected()  # re-anchor after the get
            try:
                await self._tracked(
                    "kv.update",
                    kv.update(
                        kv_key, updated.model_dump_json().encode(), last=entry.revision
                    ),
                )
            except KeyWrongLastSequenceError:
                continue
            return updated
        msg = f"session {edit.key} changed concurrently; gave up after {_TRIES} tries"
        raise SessionChangedError(msg)
