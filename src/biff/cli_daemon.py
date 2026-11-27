"""Run one CLI command as a client of biffd."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Self, final

from pydantic import BaseModel, ConfigDict, JsonValue

from biff._stdlib import expand_bead_id
from biff.cli_session_select import SessionSelector
from biff.cli_session_store import CliSessionEndedError, CliSessionRecord
from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.commands import CommandResult
from biff.config import load_cli_config
from biff.plan_marker import PlanMarker
from biff.run_dir import RunDir

__all__ = ["DaemonCommand"]

_PENDING_TTY = "pending"


class _WireResult(BaseModel):
    """What the daemon answers a command with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data: JsonValue
    error: bool
    text: str
    more: int = 0


@final
class DaemonCommand:
    """One invocation of the ``biff`` command: pick its session, ask biffd, print.

    The invocation attaches to a stored CLI session by the ladder it always
    had (an explicit name, the repo's pointed session, else a new one) and
    asks the daemon to run the command as that session.  The daemon owns the
    relay connection; this process holds none.
    """

    FAILURES = (ValueError, OSError, TimeoutError, DaemonError)
    """What a command fails with: a bad session, or an absent or refusing biffd."""

    __slots__ = ("_cwd", "_ref", "_run_dir", "_selector", "_session_id")

    _cwd: str
    _ref: str | None
    _run_dir: RunDir
    _selector: SessionSelector
    _session_id: str

    def __new__(
        cls, *, user_override: str | None, session_ref: str | None, run_dir: RunDir
    ) -> Self:
        """Resolve who this invocation is and which repository it is in."""
        self = super().__new__(cls)
        resolved = load_cli_config(user_override=user_override)
        self._selector = SessionSelector.from_resolved(resolved)
        self._ref = session_ref
        self._cwd = str(Path.cwd())
        self._run_dir = run_dir
        self._session_id = ""
        return self

    @classmethod
    def for_this_process(
        cls, user_override: str | None, session_ref: str | None
    ) -> Self:
        """Return the invocation at the default run directory."""
        return cls(
            user_override=user_override,
            session_ref=session_ref,
            run_dir=RunDir.default(),
        )

    async def call(self, method: str, **params: JsonValue) -> CommandResult:
        """Run *method* in biffd as this invocation's session and return its result.

        Raises:
            ValueError: A named session cannot be used.
            ConnectionError: biffd is absent, unproven or went away.
            DaemonError: biffd refused the request.
        """
        wire = await self._ask(method, **params)
        return CommandResult(text=wire.text, json_data=wire.data, error=wire.error)

    async def _ask(self, method: str, **params: JsonValue) -> _WireResult:
        record, is_new = await self._attach()
        self._session_id = record.session_id
        client = await DaemonClient.connect(
            run_dir=self._run_dir,
            identity=ClientIdentity(
                client="cli",
                session_id=record.session_id,
                cwd=self._cwd,
                user=record.user,
            ),
        )
        try:
            if is_new:
                await self._name(record, client)
            raw = await client.request(method, **params)
        finally:
            await client.close()
        return _WireResult.model_validate(raw)

    async def plan(self, message: str) -> CommandResult:
        """Set the plan, then sync the plan-gate marker in this process.

        The marker is keyed on this repository and the harness session, which
        biffd does not have.  A bead id expands here for the same reason.
        """
        message = expand_bead_id(message)
        wire = await self._ask("plan", message=message)
        if not wire.error:
            PlanMarker.sync(message, PlanMarker.declared_session() or self._session_id)
        return CommandResult(text=wire.text, json_data=wire.data, error=wire.error)

    async def tty(self, name: str) -> CommandResult:
        """Name the session, then keep the stored record's name in step."""
        wire = await self._ask("tty", name=name)
        if not wire.error and isinstance(wire.data, dict):
            claimed = wire.data.get("tty")
            if isinstance(claimed, str):
                await self._rename_record(claimed)
        return CommandResult(text=wire.text, json_data=wire.data, error=wire.error)

    async def _attach(self) -> tuple[CliSessionRecord, bool]:
        """Return the stored session to run as, and whether it was just made."""
        sel = self._selector
        if self._ref is not None:
            return await self._renewed(sel.explicit(self._ref)), False
        pointed = sel.pointed()
        if pointed is not None:
            try:
                return await self._renewed(pointed), False
            except (CliSessionEndedError, FileNotFoundError):
                pass  # ended or expired since the pointer was read: make a new one
        return self._create(), True

    async def _renewed(self, record: CliSessionRecord) -> CliSessionRecord:
        store = self._selector.store
        async with store.lifecycle(record.session_id):
            return store.attach(record.session_id, now=datetime.now(UTC))

    def _create(self) -> CliSessionRecord:
        """Store a new session, named once biffd has claimed its tty name."""
        sel = self._selector
        record = CliSessionRecord.new(
            tty=_PENDING_TTY,
            user=sel.user,
            repo=sel.repo,
            repo_root=str(sel.repo_root),
            hostname=sel.hostname,
            now=datetime.now(UTC),
        )
        sel.store.create(record)
        sel.pointer.write(sel.repo_root, record.session_id)
        return record

    async def _name(self, record: CliSessionRecord, client: DaemonClient) -> None:
        """Copy the tty name biffd claimed for a new session into its record."""
        status = _WireResult.model_validate(await client.request("status"))
        if isinstance(status.data, dict) and isinstance(
            name := status.data.get("tty_name"), str
        ):
            await self._rename_record(name, record.session_id)

    async def _rename_record(self, tty: str, session_id: str | None = None) -> None:
        target = session_id or self._session_id
        store = self._selector.store
        async with store.lifecycle(target):
            store.rename(target, tty)
