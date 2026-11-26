# Library API

The ten product commands (`who`, `finger`, `write`, `read`, `plan`, `last`,
`wall`, `mesg`, `tty`, `status`) are importable as async functions from
`biff.commands`. Each takes a `CliContext` and returns a `CommandResult`.
The `biff` CLI and REPL call the same functions.

## Example

This example uses `LocalRelay`, which stores state in a local directory
instead of connecting to NATS. It is the relay the test suite uses.

```python
import asyncio
import tempfile
from pathlib import Path

from biff import BiffConfig, CliContext, CommandResult, commands
from biff.relay import LocalRelay


async def main() -> None:
    relay = LocalRelay(Path(tempfile.mkdtemp()))
    config = BiffConfig(user="kai", repo_name="myapp")
    ctx = CliContext(
        relay=relay, config=config, session_key="kai:abc123", user="kai", tty="abc123"
    )

    result: CommandResult = await commands.who(ctx)
    print(result.text)  # human-readable output
    print(result.json_data)  # JSON-serializable data
    print(result.error)  # True if the command failed


asyncio.run(main())
```

## Using the Real Relay

`biff.config.load_cli_config()` resolves configuration the same way the CLI
does: `.punt-labs/biff/config.yaml` if present, otherwise the zero-config
defaults. `biff.cli_session.cli_session()` is an async context manager that
connects to the configured NATS relay, registers a session, yields a
`CliContext`, and logs the session out on exit:

```python
from biff import commands
from biff.cli_session import cli_session


async def who() -> str:
    async with cli_session() as ctx:
        result = await commands.who(ctx)
        return result.text
```

See [DES-022](../../DESIGN.md#des-022-library-api--command-extraction-via-humble-object-pattern)
in the design log for the architecture.
