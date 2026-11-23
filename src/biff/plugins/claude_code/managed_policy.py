"""The effective managed policy for channels, and the gate it can close."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import Final, Self, cast, final

from biff.plugins.claude_code.channel_gates import ClosedGate
from biff.plugins.claude_code.managed_sources import ManagedSources, Source

__all__ = ["ManagedPolicy", "ManagedPolicyGate"]

logger = logging.getLogger(__name__)

# The marketplace and plugin name Claude Code knows biff by.
BIFF_ENTRY: Final = "biff@punt-labs"

_CONTROL_KEYS = frozenset({"managedSourcesBehavior", "wslInheritsWindowsSettings"})


@final
class ManagedPolicy:
    """The managed settings that apply to channels.

    The first source with a policy key wins whole.  With
    ``managedSourcesBehavior`` set to ``"merge"`` every source with a policy
    key is merged instead, higher ranks over lower.  A source holding only
    control keys is no policy.  Server-managed settings are invisible to a
    hook, so an organization that sets channels only there has no policy here.
    """

    __slots__ = ("_effective",)

    _effective: Source

    def __new__(cls, sources: Sequence[Source]) -> Self:
        """Rank *sources*, best first, into one effective policy."""
        self = super().__new__(cls)
        with_policy = [s for s in sources if not _CONTROL_KEYS.issuperset(s)]
        effective: Source = {}
        if with_policy:
            if with_policy[0].get("managedSourcesBehavior") == "merge":
                for source in reversed(with_policy):
                    effective = ManagedSources.merged(effective, source)
            else:
                effective = with_policy[0]
        self._effective = effective
        return self

    @classmethod
    def for_machine(cls, platform: str | None = None) -> Self:
        """Return the policy visible on this machine."""
        return cls(
            ManagedSources.for_platform(sys.platform if platform is None else platform)
        )

    @property
    def channels_enabled(self) -> object:
        """The effective ``channelsEnabled`` value, or ``None`` when unset."""
        return self._effective.get("channelsEnabled")

    @property
    def has_allowlist(self) -> bool:
        """Whether a visible source sets ``allowedChannelPlugins`` at all."""
        return isinstance(self._effective.get("allowedChannelPlugins"), list)

    @property
    def lists_biff(self) -> bool:
        """Whether the effective ``allowedChannelPlugins`` names biff."""
        listed = self._effective.get("allowedChannelPlugins")
        if not isinstance(listed, list):
            return False
        return any(
            self._entry_name(entry) == BIFF_ENTRY
            for entry in cast("list[object]", listed)
        )

    @staticmethod
    def _entry_name(entry: object) -> str:
        """Return an allowlist entry as ``plugin@marketplace``."""
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            fields = cast("dict[str, object]", entry)
            plugin = fields.get("plugin")
            marketplace = fields.get("marketplace")
            if isinstance(plugin, str) and isinstance(marketplace, str):
                return f"{plugin}@{marketplace}"
        return ""


@final
class ManagedPolicyGate:
    """Unknown when the effective policy sets ``channelsEnabled`` to a non-true value.

    A server-managed setting outranks every local source and a hook cannot see
    it, so a local value is never proof: the gate tells only the model.  An
    unset key is no finding: Pro and Max users have no managed policy, and
    Claude Code's own default applies.
    """

    __slots__ = ("_policy",)

    _policy: ManagedPolicy

    def __new__(cls, policy: ManagedPolicy) -> Self:
        """Check *policy*."""
        self = super().__new__(cls)
        self._policy = policy
        return self

    def closed(self) -> tuple[ClosedGate, ...]:
        """Return the closed gate when ``channelsEnabled`` is set and not true."""
        value = self._policy.channels_enabled
        if value is None or value is True:
            return ()
        if isinstance(value, str) and value.strip().lower() == "true":
            logger.info(
                "managed channelsEnabled is the string %r; treating as true", value
            )
            return ()
        return (
            ClosedGate(
                reason=(
                    f"managed settings set channelsEnabled to {value!r} locally; "
                    "a server-managed policy may still allow channels"
                ),
                fix="",
                notify_user=False,
                unknown=True,
            ),
        )
