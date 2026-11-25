"""The channel-flag spellings the tier can launch Claude Code with."""

from __future__ import annotations

from tests.test_claude_channels.rig import Launch

LAUNCHES = {
    launch.name: launch
    for launch in (
        Launch("inline", "plugin:biff-dev@inline", True),
        Launch("marketplace", "plugin:biff-dev@punt-labs", True),
        Launch("bare-plugin", "plugin:biff-dev", True),
        Launch("server-tty", "server:tty", True),
        Launch("server-plugin-tty", "server:plugin:biff-dev:tty", True),
    )
}
DEFAULT_LAUNCH = "inline"


def select_launches(raw: str | None) -> list[str]:
    """Return the launch names *raw* (``BIFF_CHANNEL_LAUNCHES``) selects.

    Unset means the verified launch.  A blank value or an unknown name raises:
    selecting nothing would otherwise be a green run that proved nothing.
    """
    if raw is None:
        return [DEFAULT_LAUNCH]
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        msg = f"BIFF_CHANNEL_LAUNCHES is blank; choose from {sorted(LAUNCHES)}"
        raise ValueError(msg)
    unknown = sorted(set(names) - set(LAUNCHES))
    if unknown:
        msg = (
            f"unknown launches {unknown} in BIFF_CHANNEL_LAUNCHES; "
            f"choose from {sorted(LAUNCHES)}"
        )
        raise ValueError(msg)
    return names
