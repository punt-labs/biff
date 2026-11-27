"""Post a wall straight to the relay, for a CI runner that has no biffd."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from biff import commands
from biff.cli_context import CliContext
from biff.commands import CommandResult
from biff.config import load_cli_config
from biff.nats_relay import NatsRelay
from biff.relay_errors import RELAY_ERRORS
from biff.relay_select import RelaySelector
from biff.tty import build_session_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.relay import Relay

__all__ = ["DirectAnswer", "DirectWall"]

_RUNNER_TTY = "ci"


@final
class DirectAnswer:
    """A wall result that needs no confirmation from anyone."""

    __slots__ = ("_result",)

    _result: CommandResult

    def __new__(cls, result: CommandResult) -> Self:
        """Hold *result*, which was already published."""
        self = super().__new__(cls)
        self._result = result
        return self

    @property
    def result(self) -> CommandResult:
        """What the command prints."""
        return self._result

    async def deliver(self, show: Callable[[CommandResult], None]) -> None:
        """Show the result; there is nothing to confirm."""
        show(self._result)


@final
class DirectWall:
    """The one command that holds a relay connection of its own.

    A CI runner posting a build failure has no session, no inbox and no daemon,
    and its credentials arrive in ``BIFF_RELAY_*``, which biffd cannot carry.  It
    runs the wall command against the relay it was handed and closes it: post
    and clear reach the repositories the configuration names plus those its
    organizations contain, and show reads the configured repository's wall.
    Nothing else about biff takes this path.  A relay that refuses the write is
    an error result, never a quiet success.
    """

    __slots__ = ("_user",)

    _user: str | None

    def __new__(cls, user_override: str | None) -> Self:
        """Post as *user_override*, else the configured user."""
        self = super().__new__(cls)
        self._user = user_override
        return self

    @classmethod
    def for_this_process(
        cls, user_override: str | None, session_ref: str | None
    ) -> Self:
        """Return the command for this process; a runner has no named session."""
        if session_ref is not None:
            msg = "a wall posted with relay settings in the environment has no session"
            raise ValueError(msg)
        return cls(user_override)

    async def wall(self, message: str, duration: str, *, clear: bool) -> DirectAnswer:
        """Post, read or clear the wall on the configured relay."""
        resolved = load_cli_config(user_override=self._user)
        config = resolved.config
        relay = RelaySelector(config, resolved.data_dir).select(dormant=False)
        try:
            ctx = CliContext(
                relay=relay,
                config=config,
                session_key=build_session_key(config.user, _RUNNER_TTY),
                user=config.user,
                tty=_RUNNER_TTY,
                org_repos=await self._org_repos(relay, config.orgs),
            )
            result = await commands.wall(ctx, message, duration, clear=clear)
        except RELAY_ERRORS as exc:
            text = f"wall not delivered: {exc}"
            result = CommandResult(text=text, json_data={"error": text}, error=True)
        finally:
            await relay.close()
        return DirectAnswer(result)

    @staticmethod
    async def _org_repos(relay: Relay, orgs: tuple[str, ...]) -> frozenset[str]:
        """Return the repositories of *orgs*; only a NATS relay can tell."""
        if orgs and isinstance(relay, NatsRelay):
            return await relay.discover_repos_for_orgs(orgs)
        return frozenset[str]()
