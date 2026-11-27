"""Set a session's plan on behalf of a person or of a hook."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Self, final

from pydantic import ValidationError

from biff.commands._result import CommandResult
from biff.commands._session import update_current_session
from biff.formatting import format_talk_echo
from biff.models import PLAN_MAX_LENGTH, validate_plan
from biff.session_edit import SessionEdit

if TYPE_CHECKING:
    from biff.cli_context import CliContext
    from biff.models import UserSession

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

    __slots__ = ("_ctx", "_invalid", "_kept", "_message", "_source")

    _ctx: CliContext
    _message: str
    _source: PlanSource
    _kept: str
    _invalid: bool

    def __new__(cls, ctx: CliContext, message: str, source: PlanSource) -> Self:
        """Prepare to set *message* as *source* for the session of *ctx*."""
        self = super().__new__(cls)
        self._ctx = ctx
        self._message = message
        self._source = source
        self._kept = ""
        self._invalid = False
        return self

    async def run(self) -> CommandResult:
        """Write the plan, or say which manual plan was kept instead.

        The guard and the write are one compare-and-set on the row, so a manual
        plan that lands after an auto write read the row is seen on the retry,
        never overwritten.
        """
        row = await self._ctx.relay.update_session_if(
            SessionEdit(self._ctx.session_key, self._change)
        )
        if self._kept:
            return CommandResult(
                text=format_talk_echo("Plan unchanged (manual):", self._kept),
                json_data={"plan": self._kept, "unchanged": True},
            )
        if self._invalid:
            return self._too_long()
        if row is None:  # the row vanished: rebuild it with the plan in it
            await update_current_session(
                self._ctx, plan=self._message, plan_source=self._source
            )
        return CommandResult(
            text=format_talk_echo("Plan:", self._message),
            json_data={"plan": self._message},
        )

    def _change(self, row: UserSession) -> UserSession:
        """Return *row* with the plan set, or *row* itself when it must stay.

        It runs once per attempt of the compare-and-set, so it decides afresh.
        """
        self._kept = ""
        self._invalid = False
        if self._source == "auto" and row.plan_source == "manual" and row.plan:
            self._kept = row.plan
            return row
        try:
            validate_plan(self._message)
        except ValidationError:
            self._invalid = True
            return row
        now = datetime.now(UTC)
        return row.model_copy(
            update={
                "plan": self._message,
                "plan_source": self._source,
                "last_active": now,
                "last_tool_at": now,
            }
        )

    def _too_long(self) -> CommandResult:
        error = (
            f"Plan too long: {len(self._message)} characters (max {PLAN_MAX_LENGTH})"
        )
        return CommandResult(text=error, json_data={"error": error}, error=True)
