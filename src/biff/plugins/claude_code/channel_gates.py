"""The channel gates a hook can read: what closes one, and how to open it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, Self, final

__all__ = ["ChannelGate", "ClosedGate", "FeatureFlagGate"]

_TRUTHY = frozenset({"1", "true", "yes", "on"})
"""Values Claude Code reads as "set" for a boolean environment variable."""


@dataclass(frozen=True, slots=True)
class ClosedGate:
    """One reason channels cannot be live, and what the user does about it."""

    reason: str
    fix: str


class ChannelGate(Protocol):
    """A condition Claude Code needs before it registers biff as a channel."""

    def closed(self) -> tuple[ClosedGate, ...]:
        """Return every way this gate is closed; empty when it is open."""
        ...


@final
class FeatureFlagGate:
    """Open while one opt-out environment variable is not set.

    Claude Code evaluates the feature flag that enables channels at start-up,
    and each of these opt-outs stops that evaluation (DES-063).
    """

    NAMES = (
        "DISABLE_TELEMETRY",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DO_NOT_TRACK",
        "DISABLE_GROWTHBOOK",
    )
    """The environment variables that close the gate."""

    __slots__ = ("_env", "_name")

    _env: Mapping[str, str]
    _name: str

    def __new__(cls, name: str, env: Mapping[str, str]) -> Self:
        """Watch *name* in *env*."""
        self = super().__new__(cls)
        self._name = name
        self._env = env
        return self

    @classmethod
    def all_in(cls, env: Mapping[str, str]) -> tuple[Self, ...]:
        """Return one gate per opt-out variable, all reading *env*."""
        return tuple(cls(name, env) for name in cls.NAMES)

    def closed(self) -> tuple[ClosedGate, ...]:
        """Return the closed gate when the variable is set to a true value."""
        if self._env.get(self._name, "").strip().lower() not in _TRUTHY:
            return ()
        return (
            ClosedGate(
                reason=f"{self._name} is set",
                fix=f"Unset {self._name} and restart Claude Code.",
            ),
        )
