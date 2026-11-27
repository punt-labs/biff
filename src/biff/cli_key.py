"""``biff key``: the command-line adapter over :class:`KeyCommands`."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Self, final

import typer

from biff.commands import CommandResult
from biff.commands.key import KeyCommands
from biff.config import find_git_root
from biff.identity import resolve_human_identity
from biff.key_store import CallerKind, KeyStore
from biff.key_wiring import machine_vaults
from biff.nostr_directory import NostrDirectory


@final
class KeyCli:
    """Builds the ``biff key`` command group.

    Each command runs locally and opens no relay session.  The global
    ``--json``, ``--quiet`` and ``--user`` flags are read from the root
    command's parameters.  The commands are built per call by *commands*, so a
    test hands in a stand-in and a person's shell gets this machine's vaults.
    """

    __slots__ = ("_commands",)

    _commands: Callable[[], KeyCommands]

    def __new__(cls, commands: Callable[[], KeyCommands]) -> Self:
        """Build the commands to run with *commands*, once per invocation."""
        self = super().__new__(cls)
        self._commands = commands
        return self

    @classmethod
    def on_this_machine(cls) -> Self:
        """Return the group over this machine's vaults, directory and clock."""
        return cls(cls._machine_commands)

    def typer_app(self) -> typer.Typer:
        """Return the ``key`` command group."""
        group = typer.Typer(help="Create Nostr keys and attest agents to their owner.")
        group.command("init")(self.init)
        group.command("agent")(self.agent)
        group.command("attest")(self.attest)
        group.command("show")(self.show)
        return group

    def init(
        self,
        ctx: typer.Context,
        owner: Annotated[
            str | None, typer.Option(help="Your handle; default is your GitHub login")
        ] = None,
    ) -> None:
        """Create your owner key, keep it out of agents' reach and print your npub."""
        self._emit(ctx, self._commands().init(self._owner(ctx, owner)))

    def agent(
        self,
        ctx: typer.Context,
        handle: Annotated[str, typer.Argument(help="The agent's handle, e.g. claude")],
    ) -> None:
        """Create an agent's key if it has none and publish its npub."""
        self._emit(ctx, self._commands().agent(handle))

    def attest(
        self,
        ctx: typer.Context,
        agent: Annotated[str, typer.Argument(help="The agent's handle, e.g. claude")],
        owner: Annotated[str | None, typer.Option(help="Your handle")] = None,
        npub: Annotated[
            str | None,
            typer.Option(
                help="Confirm an agent key read from the directory (not held here)"
            ),
        ] = None,
        hours: Annotated[
            int | None, typer.Option(help="Lifetime; default 24, or 168 with --image")
        ] = None,
        *,
        image: Annotated[
            bool, typer.Option("--image", help="For an agent image: default to 7 days")
        ] = False,
    ) -> None:
        """Sign an agent's key as its owner; you must be present to approve."""
        result = self._commands().attest(
            self._owner(ctx, owner), agent, hours=hours, image=image, npub=npub
        )
        self._emit(ctx, result)

    def show(
        self,
        ctx: typer.Context,
        handle: Annotated[str, typer.Argument(help="Whose key to show")],
    ) -> None:
        """Show a published key and the state of its owner attestation."""
        self._emit(ctx, self._commands().show(handle))

    @staticmethod
    def _machine_commands() -> KeyCommands:
        """Build the commands for this process, this machine and this clock."""
        owner_vault, agent_vault = machine_vaults(Path.home())
        store = KeyStore(owner_vault, agent_vault, CallerKind.current())
        return KeyCommands(store, NostrDirectory.for_repo(find_git_root()), time.time)

    @staticmethod
    def _owner(ctx: typer.Context, owner: str | None) -> str:
        """Return *owner*, else the human identity of this shell."""
        user = ctx.find_root().params.get("user")
        root = find_git_root() or Path.cwd()
        return owner or resolve_human_identity(user, root)[0]

    @staticmethod
    def _emit(ctx: typer.Context, result: CommandResult) -> None:
        """Print *result* as the root flags ask and exit 1 when it failed."""
        flags = ctx.find_root().params
        if flags.get("json_flag"):
            data = result.json_data if result.json_data is not None else result.text
            typer.echo(json.dumps(data))
        elif result.error:
            typer.echo(result.text, err=True)
        elif not flags.get("quiet"):
            typer.echo(result.text)
        if result.error:
            raise typer.Exit(code=1)
