"""Configure diagnostics before dispatching the lightweight hook executable."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from punt_observe.logging import LoggingSetupError, Role

from biff.logging_config import configure_logging
from biff.plugins.claude_code._hook_dispatch import HookDispatch


@dataclass(frozen=True, slots=True)
class HookInvocation:
    """Configure and dispatch one hook invocation."""

    detached: bool

    @classmethod
    def main(cls) -> None:
        """Run the hook selected by the process arguments."""
        cls(detached=sys.argv[1:3] == ["claude-code", "session-end"]).run()

    def run(self) -> None:
        """Run the hook, retaining detached cleanup outcomes in the durable sink."""
        try:
            configure_logging(Role.WORKER if self.detached else Role.HOOK)
        except LoggingSetupError as exc:
            # A diagnostic outage must not bypass the edit gate or caller stamping.
            sys.stderr.write(f"biff-hook: {exc}\n")
        HookDispatch.run(detached=self.detached)


main = HookInvocation.main
