"""Who holds a port, as far as the host will say."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_address
from typing import ClassVar, Final, final

from biff.supervision.runner import Supervisor

__all__ = ["Holder", "PortHolder"]

_LOOKUP_SECONDS: Final = 2.0
_LOOPBACK: Final = IPv4Address("127.0.0.1")


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
    """Finds the listener on 127.0.0.1's port with ``lsof``, else ``ss``.

    A guess is worse than silence, so a tool that is missing, hangs, is refused,
    prints a name that does not decode, or names no listener on that address gives
    no holder, and nothing here raises.
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
            except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
                continue
            holder = parse(result.stdout)
            if holder is not None:
                return holder
        return None

    @staticmethod
    def _covers(endpoint: str) -> bool:
        """Return whether a listener at ``host:port`` covers 127.0.0.1.

        The probe connects to 127.0.0.1, so a listener on another address, such as
        ``[::1]``, is not the one that answered.  A wildcard listener covers it.
        """
        host = endpoint.rpartition(":")[0]
        if host == "*":
            return True
        try:
            address = ip_address(host.strip("[]"))
        except ValueError:
            return False
        return address.is_unspecified or address == _LOOPBACK

    @classmethod
    def _from_lsof(cls, text: str) -> Holder | None:
        """Read ``lsof -Fpcn``: ``p<pid>``, ``c<command>``, then ``n<host:port>``s."""
        pid = ""
        command = ""
        covers = False
        for line in [*text.splitlines(), "p"]:
            tag, value = line[:1], line[1:]
            if tag == "p":
                if covers and command and pid.isdecimal():
                    return Holder(command, int(pid))
                pid, command, covers = value, "", False
            elif tag == "c" and not command:
                command = value
            elif tag == "n" and cls._covers(value):
                covers = True
        return None

    @classmethod
    def _from_ss(cls, text: str) -> Holder | None:
        """Read ``ss -p`` lines: the local address and the ``users:`` field."""
        for line in text.splitlines():
            fields = line.split()
            found = cls._SS_USER.search(line)
            if len(fields) > 3 and cls._covers(fields[3]) and found:
                return Holder(found.group(1), int(found.group(2)))
        return None
