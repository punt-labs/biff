"""Set a session's plan on behalf of a person or of a hook."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self, final

from pydantic import ValidationError

from biff.commands._result import CommandResult
from biff.commands._session import update_current_session
from biff.formatting import format_talk_echo
from biff.models import PLAN_MAX_LENGTH, validate_plan

if TYPE_CHECKING:
    from biff.cli_context import CliContext

__all__ = ["PLAN_SOURCES", "PlanSource", "PlanWrite"]

type PlanSource = Literal["manual", "auto"]
PLAN_SOURCES: tuple[PlanSource, ...] = ("manual", "auto")


@final
class PlanWrite:
    """One plan write: *message* set as *source* would set it.

    A hook sets ``auto``: its plan never replaces a non-empty manual one, and
    the reply says so (``unchanged``), so the caller still rewrites the
    plan-gate marker that a new session clears.

    What only the calling process can do is the caller's: expanding a bead id
    and writing that marker (:class:`biff.plan_marker.PlanMarker`).
    """

    __slots__ = ("_ctx", "_message", "_source")

    _ctx: CliContext
    _message: str
    _source: PlanSource

    def __new__(cls, ctx: CliContext, message: str, source: PlanSource) -> Self:
        """Prepare to set *message* as *source* for the session of *ctx*."""
        self = super().__new__(cls)
        self._ctx = ctx
        self._message = message
        self._source = source
        return self

    async def run(self) -> CommandResult:
        """Write the plan, or say which manual plan was kept instead."""
        kept = await self._kept()
        if kept:
            return CommandResult(
                text=format_talk_echo("Plan unchanged (manual):", kept),
                json_data={"plan": kept, "unchanged": True},
            )
        try:
            validate_plan(self._message)
        except ValidationError:
            return self._too_long()
        await update_current_session(
            self._ctx, plan=self._message, plan_source=self._source
        )
        return CommandResult(
            text=format_talk_echo("Plan:", self._message),
            json_data={"plan": self._message},
        )

    async def _kept(self) -> str:
        """Return the manual plan an auto write must keep, or an empty string."""
        if self._source != "auto":
            return ""
        session = await self._ctx.relay.get_session(self._ctx.session_key)
        if session is not None and session.plan_source == "manual":
            return session.plan
        return ""

    def _too_long(self) -> CommandResult:
        error = (
            f"Plan too long: {len(self._message)} characters (max {PLAN_MAX_LENGTH})"
        )
        return CommandResult(text=error, json_data={"error": error}, error=True)
