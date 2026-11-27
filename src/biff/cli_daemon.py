"""Run one CLI command as a client of biffd."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, final

from biff._stdlib import expand_bead_id
from biff.cli_daemon_wire import UnreadableReplyError, WireResult
from biff.cli_session_select import SessionSelector
from biff.cli_session_store import CliSessionEndedError, CliSessionRecord
from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.client.nudge import DaemonNudge
from biff.config import load_cli_config
from biff.plan_marker import PlanMarker
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

    from biff.client.held_reply import HeldReply
    from biff.commands import CommandResult

__all__ = ["Answer", "DaemonCommand", "Delivery", "ResultPrinter"]

_PENDING_TTY = "pending"
_NOT_NAMED = "biffd did not name this session; run: biff doctor"


class Delivery(Protocol):
    """A command's result and the means to show it; ``Answer`` is the real one."""

    @property
    def result(self) -> CommandResult:
        """What the command prints."""
        raise NotImplementedError

    async def deliver(self, show: Callable[[CommandResult], None]) -> None:
        """Show the result with *show*."""


@final
class ResultPrinter:
    """Prints a result as text or JSON, flushing so a closed pipe fails here."""

    __slots__ = ("_json", "_quiet")

    _json: bool
    _quiet: bool

    def __new__(cls, *, json_output: bool, quiet: bool) -> Self:
        """Print JSON when *json_output*, nothing but errors when *quiet*."""
        self = super().__new__(cls)
        self._json = json_output
        self._quiet = quiet
        return self

    def __call__(self, result: CommandResult) -> None:
        """Print *result* and flush stdout."""
        if self._json:
            data = result.json_data if result.json_data is not None else result.text
            sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")
        elif result.error:
            sys.stderr.write(result.text + "\n")
        elif not self._quiet:
            sys.stdout.write(result.text + "\n")
        sys.stdout.flush()


@final
class Answer:
    """A command's result, which biffd counts delivered only once it was shown.

    Mail a ``read`` carries stays unread until :meth:`delivered`, so a result
    that fails to print, or to parse, loses nothing.
    """

    __slots__ = ("_client", "_held", "_wire")

    _client: DaemonClient
    _held: HeldReply
    _wire: WireResult

    def __new__(cls, wire: WireResult, held: HeldReply, client: DaemonClient) -> Self:
        """Hold *wire*, whose confirmation *held* still owes, on *client*."""
        self = super().__new__(cls)
        self._wire = wire
        self._held = held
        self._client = client
        return self

    @property
    def data(self) -> JsonValue:
        """The command's JSON payload."""
        return self._wire.data

    @property
    def result(self) -> CommandResult:
        """What the command prints."""
        return self._wire.command_result()

    async def deliver(self, show: Callable[[CommandResult], None]) -> None:
        """Show the result with *show*, then confirm it; confirm nothing if it fails."""
        try:
            show(self.result)
        except BaseException:
            await self.abandon()
            raise
        try:
            await self._held.confirm()
        finally:
            await self._client.close()

    async def abandon(self) -> None:
        """Close without confirming: whatever the reply carried stays undone."""
        await self._client.close()


@final
class DaemonCommand:
    """One invocation of the ``biff`` command: pick its session, ask biffd, print.

    The invocation attaches to a stored CLI session (an explicit name, the
    repo's pointed session, else a new one) and asks the daemon to run the
    command as that session.  The daemon owns the relay connection; this
    process holds none, and starts the daemon first when it is down.
    """

    FAILURES = (ValueError, OSError, TimeoutError, DaemonError)
    """What a command fails with: a bad session, or an absent or refusing biffd."""

    __slots__ = ("_cwd", "_nudge", "_ref", "_run_dir", "_selector", "_session_id")

    _cwd: str
    _nudge: Callable[[], int]
    _ref: str | None
    _run_dir: RunDir
    _selector: SessionSelector
    _session_id: str

    def __new__(
        cls,
        *,
        user_override: str | None,
        session_ref: str | None,
        run_dir: RunDir,
        nudge: Callable[[], int],
    ) -> Self:
        """Resolve who this invocation is; *nudge* returns the port biffd answers on."""
        self = super().__new__(cls)
        resolved = load_cli_config(user_override=user_override)
        self._selector = SessionSelector.from_resolved(resolved)
        self._ref = session_ref
        self._cwd = str(Path.cwd())
        self._run_dir = run_dir
        self._nudge = nudge
        self._session_id = ""
        return self

    @classmethod
    def for_this_process(
        cls, user_override: str | None, session_ref: str | None
    ) -> Self:
        """Return the invocation at the default run directory; biffd starts if down."""
        run_dir = RunDir.default()

        def start_biffd() -> int:
            return DaemonNudge.for_host(run_dir).ensure_running()

        return cls(
            user_override=user_override,
            session_ref=session_ref,
            run_dir=run_dir,
            nudge=start_biffd,
        )

    async def call(self, method: str, **params: JsonValue) -> Answer:
        """Run *method* in biffd as this invocation's session.

        Raises:
            ValueError: A named session cannot be used.
            ConnectionError: biffd is absent, cannot start, or sent an unreadable reply.
            DaemonError: biffd refused the request.
        """
        client, _ = await self._open()
        return await self._answer(client, method, params)

    async def plan(self, message: str) -> Answer:
        """Set the plan, then sync the plan-gate marker in this process.

        The marker is keyed on this repository and the harness session, which
        biffd does not have.  A bead id expands here for the same reason.
        """
        message = expand_bead_id(message)
        answer = await self.call("plan", message=message)
        try:
            if not answer.result.error:
                session = PlanMarker.declared_session() or self._session_id
                PlanMarker.sync(message, session)
        except BaseException:
            await answer.abandon()
            raise
        return answer

    async def tty(self, name: str) -> Answer:
        """Name the session; keep the stored record in step or undo the rename."""
        client, record = await self._open()
        answer = await self._answer(client, "tty", {"name": name})
        try:
            data = answer.data
            if not answer.result.error and isinstance(data, dict):
                await self._rename_or_undo(client, record, str(data["tty"]))
        except BaseException:
            await answer.abandon()
            raise
        return answer

    async def _answer(
        self, client: DaemonClient, method: str, params: dict[str, JsonValue]
    ) -> Answer:
        """Ask *client*; close it unless the reply parses, so nothing is confirmed."""
        try:
            held = await client.request_held(method, **params)
            return Answer(WireResult.parse(held.result), held, client)
        except BaseException:
            await client.close()
            raise

    async def _rename_or_undo(
        self, client: DaemonClient, record: CliSessionRecord, claimed: str
    ) -> None:
        """Rename the stored record; if that fails, put the relay name back."""
        try:
            async with self._selector.store.lifecycle(record.session_id):
                self._selector.store.rename(record.session_id, claimed)
        except (OSError, ValueError):
            await client.request("tty", name=record.tty)
            raise

    async def _open(self) -> tuple[DaemonClient, CliSessionRecord]:
        """Start biffd if down, connect as this session, and store it if new."""
        record, is_stored = await self._attach()
        port = await asyncio.to_thread(self._nudge)
        client = await DaemonClient.connect(
            run_dir=self._run_dir,
            port=port,
            identity=ClientIdentity(
                client="cli",
                session_id=record.session_id,
                cwd=self._cwd,
                user=record.user,
            ),
        )
        try:
            if not is_stored:
                record = await self._store_named(record, client)
        except BaseException:
            await client.close()
            raise
        self._session_id = record.session_id
        return client, record

    async def _attach(self) -> tuple[CliSessionRecord, bool]:
        """Return the session to run as, and whether it is already stored.

        A new session is only drafted here: it is stored once biffd has named it,
        so a command that never reaches biffd leaves no record behind.
        """
        sel = self._selector
        if self._ref is not None:
            return await self._renewed(sel.explicit(self._ref)), True
        pointed = sel.pointed()
        if pointed is not None:
            try:
                return await self._renewed(pointed), True
            except (CliSessionEndedError, FileNotFoundError):
                pass  # ended or expired since the pointer was read: draft a new one
        draft = CliSessionRecord.new(
            tty=_PENDING_TTY,
            user=sel.user,
            repo=sel.repo,
            repo_root=str(sel.repo_root),
            hostname=sel.hostname,
            now=datetime.now(UTC),
        )
        return draft, False

    async def _renewed(self, record: CliSessionRecord) -> CliSessionRecord:
        store = self._selector.store
        async with store.lifecycle(record.session_id):
            return store.attach(record.session_id, now=datetime.now(UTC))

    async def _store_named(
        self, draft: CliSessionRecord, client: DaemonClient
    ) -> CliSessionRecord:
        """Store *draft* under the tty name biffd claimed for it, and point at it."""
        status = WireResult.parse(await client.request("status"))
        data = status.data
        name = data.get("tty_name") if isinstance(data, dict) else None
        if status.error or not isinstance(name, str) or name in ("", _PENDING_TTY):
            raise UnreadableReplyError(_NOT_NAMED)
        sel = self._selector
        record = replace(draft, tty=name)
        sel.store.create(record)
        sel.pointer.write(sel.repo_root, record.session_id)
        return record
