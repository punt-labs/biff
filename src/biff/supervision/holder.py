"""Who holds a port, as far as the host will say."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import ClassVar, Final, final

from biff.supervision.runner import Supervisor

__all__ = ["Holder", "PortHolder"]

_LOOKUP_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class Holder:
    """A program that listens on a port."""

    command: str
    pid: int

    def describe(self) -> str:
        """Return the holder as ``command (pid N)``."""
        return f"{self.command} (pid {self.pid})"


@final
class PortHolder:
    """Finds the listener on a loopback port with ``lsof``, else ``ss``.

    A guess is worse than silence, so a tool that is missing, hangs, is refused or
    says nothing nameable gives no holder, and nothing here raises.
    """

    _SS_USER: ClassVar[re.Pattern[str]] = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')

    @classmethod
    def find(cls, port: int) -> Holder | None:
        """Return the program listening on *port*, or ``None`` when it is not known."""
        attempts = (
            (
                ("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpcn"),
                cls._from_lsof,
            ),
            (("ss", "-ltnpH", f"sport = :{port}"), cls._from_ss),
        )
        for argv, parse in attempts:
            try:
                result = Supervisor.run(*argv, timeout=_LOOKUP_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                continue
            holder = parse(result.stdout)
            if holder is not None:
                return holder
        return None

    @staticmethod
    def _from_lsof(text: str) -> Holder | None:
        """Read the first process of ``lsof -Fpcn``: ``p<pid>`` then ``c<command>``."""
        pid = ""
        command = ""
        for line in text.splitlines():
            tag, value = line[:1], line[1:]
            if tag == "p":
                if pid:
                    break
                pid = value
            elif tag == "c" and pid and not command:
                command = value
        return Holder(command, int(pid)) if command and pid.isdecimal() else None

    @classmethod
    def _from_ss(cls, text: str) -> Holder | None:
        """Read the first ``users:(("command",pid=N,...))`` of ``ss -p`` output."""
        found = cls._SS_USER.search(text)
        return Holder(found.group(1), int(found.group(2))) if found else None
