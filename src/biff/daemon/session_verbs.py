"""The ``session`` verbs: create, list, select and end named CLI sessions."""

from __future__ import annotations

from dataclasses import dataclass

from biff.commands._result import CommandResult
from biff.daemon.session_host import SessionHost
from biff.daemon.session_store import CliSessionRecord


@dataclass(frozen=True, slots=True)
class SessionVerbs:
    """The four ``session`` verbs, run against one :class:`SessionHost`."""

    host: SessionHost

    async def new(self, tty: str | None) -> CommandResult:
        """Create a session and reserve its name; the caller points the repo at it."""
        record = await self.host.create(tty)
        return CommandResult(text=record.tty, json_data=self._row(record))

    async def ls(self, *, include_ended: bool) -> CommandResult:
        """List this user's sessions on this host; *include_ended* adds ended."""
        host = self.host
        rows = [
            self._row(r)
            for r in host.selector.store.list()
            if r.user == host.config.user
            and r.hostname == host.hostname
            and (include_ended or r.state != "ended")
        ]
        if not rows:
            return CommandResult(text="No sessions.", json_data=rows)
        header = (
            f"{'NAME':<12}{'USER':<12}{'REPO':<16}{'STATE':<9}{'LAST ATTACHED':<26}ID"
        )
        lines = [
            f"{r['name']:<12}{r['user']:<12}{r['repo']:<16}{r['state']:<9}"
            f"{r['last_attached']:<26}{r['id']}"
            for r in rows
        ]
        return CommandResult(text="\n".join([header, *lines]), json_data=rows)

    async def use(self, ref: str) -> CommandResult:
        """Name the session *ref* for the caller to point at; no state change."""
        record = self.host.selector.explicit(ref)
        return CommandResult(text=record.tty, json_data=self._row(record))

    async def end(self, ref: str) -> CommandResult:
        """End *ref*, draining its inbox into the owner's mailbox first."""
        sel = self.host.selector
        record = sel.lookup(ref)
        if record.state == "ended":
            raise record.ended_error()
        sel.require_repo(record)
        ended = await self.host.end(record)
        return CommandResult(text=f"ended {ended.tty}", json_data=self._row(ended))

    @staticmethod
    def _row(record: CliSessionRecord) -> dict[str, str]:
        return {
            "name": record.tty,
            "user": record.user,
            "repo": record.repo,
            "state": record.state,
            "last_attached": record.last_attached_at.isoformat(timespec="seconds"),
            "id": record.session_id,
        }
