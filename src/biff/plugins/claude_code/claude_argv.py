"""The channel-related parts of Claude Code's command line."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Self, final

__all__ = ["ClaudeArgv"]

DEV_FLAG: Final = "--dangerously-load-development-channels"
CHANNELS_FLAG: Final = "--channels"
BIFF_CHANNEL: Final = "plugin:biff@punt-labs"
_BIFF_PREFIXES: Final = ("plugin:biff@", "plugin:biff-dev@")


@final
class ClaudeArgv:
    """What ``claude`` was started with, as far as channels are concerned.

    Both channel flags are variadic: values follow separated by spaces, joined
    by commas, or after an ``=``, and bare words after an ``=`` value are more
    values until the next option.
    """

    __slots__ = ("_channels", "_dev", "_print")

    _channels: tuple[str, ...]
    _dev: tuple[str, ...]
    _print: bool

    def __new__(cls, argv: Sequence[str]) -> Self:
        """Parse *argv*, whose first item is the program."""
        self = super().__new__(cls)
        channels: list[str] = []
        dev: list[str] = []
        current: list[str] | None = None
        is_print = False
        for arg in argv[1:]:
            flag, equals, value = arg.partition("=")
            if flag in (DEV_FLAG, CHANNELS_FLAG):
                current = dev if flag == DEV_FLAG else channels
                if equals:
                    current.extend(value.split(","))
            elif arg.startswith("-"):
                current = None
                is_print = is_print or arg in ("-p", "--print")
            elif current is not None:
                current.extend(arg.split(","))
        self._channels = tuple(channels)
        self._dev = tuple(dev)
        self._print = is_print
        return self

    @property
    def is_print(self) -> bool:
        """Whether this is a non-interactive ``-p`` run, which ignores channels."""
        return self._print

    @property
    def dev_names_biff(self) -> bool:
        """Whether the development flag lists biff or biff-dev, from any marketplace."""
        return any(value.startswith(_BIFF_PREFIXES) for value in self._dev)

    @property
    def channels_name_biff(self) -> bool:
        """Whether ``--channels`` lists biff."""
        return BIFF_CHANNEL in self._channels
