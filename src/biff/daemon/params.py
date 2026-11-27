"""A request's params, read by name and checked to be fully consumed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from biff.daemon.refusal import RefusalError
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from pydantic import JsonValue

__all__ = ["Params"]


@final
class Params:
    """A request's params, read by name and checked to be fully consumed.

    A method reads every param it accepts and then calls :meth:`done`, so a
    param it does not know, such as another session's key, is refused instead
    of silently ignored.
    """

    __slots__ = ("_method", "_raw", "_seen")

    _method: str
    _raw: dict[str, JsonValue]
    _seen: set[str]

    def __new__(cls, method: str, raw: dict[str, JsonValue]) -> Self:
        """Wrap *raw*, the params of a request for *method*."""
        self = super().__new__(cls)
        self._method = method
        self._raw = raw
        self._seen = set()
        return self

    def text(self, name: str, default: str | None = None) -> str:
        """Return the string param *name*, or *default* when absent.

        Raises:
            RefusalError: The param is absent with no default, or not a string.
        """
        self._seen.add(name)
        if name not in self._raw:
            if default is None:
                raise self._invalid(f"{name} is required")
            return default
        value = self._raw[name]
        if not isinstance(value, str):
            raise self._invalid(f"{name} must be a string")
        return value

    def integer(self, name: str, default: int) -> int:
        """Return the integer param *name*, or *default* when absent.

        Raises:
            RefusalError: The param is not an integer.
        """
        self._seen.add(name)
        value = self._raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._invalid(f"{name} must be an integer")
        return value

    def flag(self, name: str, *, default: bool) -> bool:
        """Return the boolean param *name*, or *default* when absent.

        Raises:
            RefusalError: The param is not a boolean.
        """
        self._seen.add(name)
        value = self._raw.get(name, default)
        if not isinstance(value, bool):
            raise self._invalid(f"{name} must be a boolean")
        return value

    def done(self) -> None:
        """Refuse any param the method did not read.

        Raises:
            RefusalError: A param is present that the method does not accept.
        """
        unknown = sorted(set(self._raw) - self._seen)
        if unknown:
            raise self._invalid(f"unknown param {', '.join(unknown)}")

    def _invalid(self, why: str) -> RefusalError:
        return RefusalError.of(ErrorCode.INVALID_PARAMS, f"{self._method}: {why}")
