# biff

> The dog that barked when messages arrived.

A modern CLI communication tool for software engineers, named after the Berkeley dog whose 1980 mail notification program was part of the same BSD family as `wall`, `talk`, `finger`, `write`, and `mesg`.

## Vision

Biff resurrects the UNIX communication vocabulary as MCP-native slash commands for team collaboration inside Claude Code sessions and other MCP-compatible systems.

**Purposeful, not chatty.** Every command implies intent. No channels, no threads, no emoji reactions.

## Commands

| Command | Unix Ancestor | Purpose |
|---------|---------------|---------|
| `/write @user` | `write` | Send a purposeful async message |
| `/talk @user` | `talk` | Real-time two-way conversation |
| `/wall` | `wall` | Broadcast to the team |
| `/finger @user` | `finger` | Check what someone is working on |
| `/who` | `who` / `w` | List active sessions |
| `/plan "msg"` | `.plan` | Set what you're working on |
| `/mesg on/off` | `mesg` | Control availability |
| `/share @user` | (new) | Share diffs, files, snippets |
| `/cr @user` | (new) | Request code review with context |

## Development

```bash
uv sync --extra dev        # Install dependencies
uv run ruff check .        # Lint
uv run ruff format .       # Format
uv run mypy src/ tests/    # Type check
uv run pytest              # Test
```

## License

MIT
