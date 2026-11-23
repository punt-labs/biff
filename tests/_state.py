"""``create_state`` for tests: the real registry ``cli`` builds, never an empty one."""

from __future__ import annotations

from functools import partial

from biff._cli_runtime import CliInvocation
from biff.server.state import create_state as _create_state

create_state = partial(_create_state, toggle=CliInvocation.repo_toggle())
