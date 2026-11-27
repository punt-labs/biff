"""The one service biff supervises."""

from __future__ import annotations

from typing import Final

from biff.log_hint import LOG_PATH
from biff.run_dir import DEFAULT_PORT
from biff.supervision.spec import ServiceSpec

__all__ = ["BIFFD_SPEC", "PERMANENT_EXIT_STATUS"]

PERMANENT_EXIT_STATUS: Final = 78
"""``EX_CONFIG``: biffd exits with it when no restart can help, and no unit
restarts it."""

BIFFD_SPEC: Final = ServiceSpec(
    display_name="biffd",
    launchd_label="com.punt-labs.biffd",
    systemd_unit="biffd",
    systemd_description="biff team messaging daemon",
    binary_name="biffd",
    # The port is explicit so every supervisor and every client agree on it,
    # whatever ``serve.port`` says after a crash.
    extra_args=("--port", str(DEFAULT_PORT)),
    cli_verb="daemon",
    cli_name="biff",
    process_name="biffd",
    health_port=DEFAULT_PORT,
    restart_on_crash_only=True,
    log_path=LOG_PATH,
    permanent_exit_status=PERMANENT_EXIT_STATUS,
    beside_interpreter=True,
)
