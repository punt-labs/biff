"""The channel gates a hook can read: what closes one, and how to open it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, Self, final

__all__ = ["ChannelGate", "ClosedGate", "EnvGate"]

_TRUTHY = frozenset({"1", "true", "yes", "on"})
"""Values Claude Code reads as "set" for a boolean environment variable."""

_OPT_OUTS = (
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_GROWTHBOOK",
    "DISABLE_TELEMETRY",
    "CLAUDE_CODE_DISABLE_FEATURE_FLAGS",
)
"""Variables that stop the feature-flag evaluation channels depend on (DES-063).

``DO_NOT_TRACK`` is not here: no real launch showed it closing channels.
"""

_PROVIDERS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
"""Variables that select a cloud provider; channels need Anthropic authentication."""


@dataclass(frozen=True, slots=True)
class ClosedGate:
    """One reason channels cannot be live, and what the user does about it.

    ``notify_user`` is false for a closure the user cannot act on, which only
    the model is told about.
    """

    reason: str
    fix: str
    notify_user: bool = True


class ChannelGate(Protocol):
    """A condition Claude Code needs before it registers biff as a channel."""

    def closed(self) -> tuple[ClosedGate, ...]:
        """Return every way this gate is closed; empty when it is open."""
        ...


@final
class EnvGate:
    """Open while none of a set of environment variables is set to a true value."""

    __slots__ = ("_env", "_fix", "_names", "_reason")

    _env: Mapping[str, str]
    _fix: str | None
    _names: tuple[str, ...]
    _reason: str | None

    def __new__(
        cls,
        names: tuple[str, ...],
        env: Mapping[str, str],
        *,
        reason: str | None = None,
        fix: str | None = None,
    ) -> Self:
        """Watch *names* in *env*.

        Without *reason* and *fix* the gate reports the name that is set and
        tells the user to unset it.
        """
        self = super().__new__(cls)
        self._names = names
        self._env = env
        self._reason = reason
        self._fix = fix
        return self

    @classmethod
    def opt_outs(cls, env: Mapping[str, str]) -> tuple[Self, ...]:
        """Return one gate per feature-flag opt-out variable, reading *env*."""
        return tuple(cls((name,), env) for name in _OPT_OUTS)

    @classmethod
    def provider(cls, env: Mapping[str, str]) -> Self:
        """Return the gate that closes when a cloud provider is selected."""
        return cls(
            _PROVIDERS,
            env,
            reason="channels need Anthropic authentication",
            fix="Polling is how messages arrive with this provider.",
        )

    def closed(self) -> tuple[ClosedGate, ...]:
        """Return the closed gate when a watched variable has a true value."""
        names = [
            name
            for name in self._names
            if self._env.get(name, "").strip().lower() in _TRUTHY
        ]
        if not names:
            return ()
        listed = ", ".join(f"{name} is set" for name in names)
        reason = listed if self._reason is None else f"{self._reason}, and {listed}"
        fix = (
            f"Unset {' and '.join(names)} and restart Claude Code."
            if self._fix is None
            else self._fix
        )
        return (ClosedGate(reason=reason, fix=fix),)
