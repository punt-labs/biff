# biff

> Team communication for engineers who never leave the terminal.

Named after the Berkeley dog whose 1980 mail notification program was part of the same BSD family as `talk`, `wall`, `finger`, `who`, and `mesg`.

Biff resurrects the Unix communication vocabulary as MCP-native slash commands. It runs inside your Claude Code session — no separate app, no browser tab, no context switch.

## Why

Engineers using AI coding tools are shipping faster than ever. But every time they need to coordinate with a teammate, they context-switch to Slack or Discord — tools designed for managers, not makers in deep focus. Biff keeps communication where the code already lives.

## Quick Start

```bash
pip install biff-mcp
```

Biff auto-registers as an MCP server. If your repo has a `.biff` file, it picks up the relay URL and team roster automatically. Type `/who` to see your team.

## Commands

| Command | Origin | Purpose |
|---------|--------|---------|
| `/mesg @user "text"` | BSD `mesg` | Send a message |
| `/talk @user` | BSD `talk` | Real-time bidirectional conversation |
| `/wall "text"` | BSD `wall` | Broadcast to the hive or team |
| `/finger @user` | BSD `finger` | Read someone's plan and status |
| `/who` | BSD `who` | List active sessions |
| `/plan "text"` | BSD `.plan` | Set your status |
| `/biff on` \| `off` | BSD `biff` | Control message reception |
| `/hive @a @b @c` | — | Temporary group; `/hive off` dissolves it |
| `/pair @user` | — | Invite someone to input to your Claude session |
| `/send @user` | — | Send diffs, files, or snippets |
| `/cr @user` | — | Request a code review |

## Agents Welcome

Because biff speaks MCP, it does not distinguish between human and agent sessions. An autonomous coding agent can join a `/hive`, broadcast via `/wall`, or `/mesg` a human when it needs a decision. Biff is the communication layer for the entire hive of humans and agents building software together.

## Configuration

Team configuration lives in a `.biff` file committed to your git repo:

```
relay = wss://relay.example.com
members = @kai @eric @jim
```

Clone the repo, install biff, you're connected. No account to create, no workspace to configure.

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
