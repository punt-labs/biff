"""The one service biff supervises."""

from __future__ import annotations

from typing import Final

from biff.log_hint import LOG_PATH
from biff.run_dir import DEFAULT_PORT
from biff.supervision.spec import ServiceSpec

__all__ = ["BIFFD_SPEC"]

BIFFD_SPEC: Final = ServiceSpec(
    display_name="biffd",
    launchd_label="com.punt-labs.biffd",
    systemd_unit="biffd",
    systemd_description="biff team messaging daemon",
    binary_name="biffd",
    extra_args=(),
    cli_verb="daemon",
    cli_name="biff",
    process_name="biffd",
    health_port=DEFAULT_PORT,
    restart_on_crash_only=True,
    log_path=LOG_PATH,
)
