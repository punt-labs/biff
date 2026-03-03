"""Pure async command functions for the biff CLI.

Each function takes a :class:`~biff.cli_session.CliContext` plus
command-specific arguments and returns a
:class:`~biff.commands._result.CommandResult`.  No I/O, no exit
codes — the CLI adapter in ``__main__`` handles those.

Library callers can import and await these directly::

    from biff.commands import who, finger, wall
    result = await who(ctx)
    print(result.text)
"""

from __future__ import annotations

from biff.commands._result import CommandResult
from biff.commands.finger import finger
from biff.commands.last import last
from biff.commands.mesg import mesg
from biff.commands.plan import plan
from biff.commands.read import read
from biff.commands.status import status
from biff.commands.tty import tty
from biff.commands.wall import wall
from biff.commands.who import who
from biff.commands.write import write

__all__ = [
    "CommandResult",
    "finger",
    "last",
    "mesg",
    "plan",
    "read",
    "status",
    "tty",
    "wall",
    "who",
    "write",
]
