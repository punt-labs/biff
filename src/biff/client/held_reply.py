"""A reply whose confirmation the caller sends once it has used the result."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import JsonValue

__all__ = ["HeldReply"]


@final
class HeldReply:
    """A daemon result whose confirmation, if it asks for one, is still unsent."""

    __slots__ = ("_confirm", "_result")

    _result: JsonValue
    _confirm: Callable[[], Awaitable[None]] | None  # None: the reply asked for none

    def __new__(
        cls, result: JsonValue, confirm: Callable[[], Awaitable[None]] | None
    ) -> Self:
        """Hold *result* until *confirm*, when there is one, is awaited."""
        self = super().__new__(cls)
        self._result = result
        self._confirm = confirm
        return self

    @property
    def result(self) -> JsonValue:
        """The daemon's result."""
        return self._result

    async def confirm(self) -> None:
        """Tell the daemon the result was used; a no-op when it asked for nothing."""
        if self._confirm is not None:
            await self._confirm()
