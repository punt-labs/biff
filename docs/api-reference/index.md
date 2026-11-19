# Python API reference

These typed entry points back the Biff CLI and library. See the
[library guide](https://github.com/punt-labs/biff/blob/main/docs/LIBRARY.md)
for usage examples.

::: biff
    options:
      members: false

## Commands and session context

::: biff.commands
    options:
      members: false

::: biff.cli_context.CliContext

::: biff.commands._result.CommandResult

::: biff.cli_session.cli_session

::: biff.commands.who.who

::: biff.commands.finger.finger

::: biff.commands.write.write

::: biff.commands.read.read

::: biff.commands.plan.plan

::: biff.commands.last.last

::: biff.commands.wall.wall

::: biff.commands.mesg.mesg

::: biff.commands.tty.tty

::: biff.commands.status.status

## Configuration and models

::: biff.config.load_cli_config

::: biff.config.load_mcp_config

::: biff.models.BiffConfig

::: biff.models.Message

::: biff.models.UnreadSummary

::: biff.models.UserSession

::: biff.models.WallPost

## Relays

::: biff.relay.Relay

::: biff.relay.LocalRelay

::: biff.relay.DormantRelay

::: biff.nats_relay.NatsRelay
