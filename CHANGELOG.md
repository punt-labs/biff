# Changelog

## Unreleased

### Added

- **MCP server** — FastMCP-based server with HTTP and stdio transports,
  serving tools modeled on BSD commands: `write`, `read_messages`, `finger`,
  `who`, `plan`, `mesg` (#5, #11)
- **Data models** — frozen pydantic models for messages, sessions, and
  presence (#3)
- **Storage layer** — JSONL message store and JSON session store (#4)
- **Relay protocol** — pluggable relay abstraction with `.biff` config file;
  `LocalRelay` for single-machine use (#15)
- **NATS relay** — `NatsRelay` with JetStream messaging and KV-backed
  sessions, automatic relay selection based on `relay_url` config (#16, #17)
- **Remote NATS** — token, NKey seed, and credentials file auth via `.biff`
  `[relay]` section; TLS via `tls://` URL scheme; automatic reconnection with
  disconnect/reconnect/error logging (#19)
- **Dynamic descriptions** — `read_messages` tool description updates with
  unread count and preview after every tool call; fires `tools/list_changed`
  notification (#13, #21)
- **Status bar** — `biff install-statusline` / `biff uninstall-statusline` CLI
  commands that configure Claude Code's status line with per-project unread
  counts and register the biff MCP server (#14, #22, #23, #24)
- **Claude Code plugin** — slash commands (`/who`, `/read`, `/finger`,
  `/write`, `/plan`, `/on`, `/off`) with PostToolUse hook for formatted
  output (#29, #30, #31)
- **Unix-style output** — columnar table format for `/who` and `/read`,
  BSD `finger(1)` layout for `/finger`, with `▶` header alignment for
  Claude Code UI (#31)
- **GitHub identity** — resolve display name from `gh api user` for
  `/finger` output (#27)
- **Session history** — `/last` command showing login/logout history modeled
  after Unix `last(1)`. NATS wtmp stream (JetStream, 30-day retention) records
  session events. Three-layer logout: sentinel-based (SIGTERM), orphan detection
  (crash recovery at startup), and KV watcher (TTL expiry). Per-user filtering,
  configurable count, columnar output with duration (#49)

### Testing

- **Integration tests** — two MCP clients over `FastMCPTransport` testing
  tool discovery, presence, and cross-user state (#7, #8)
- **Subprocess tests** — real `biff` subprocesses over `StdioTransport`
  verifying wire protocol, CLI args, and cross-process state (#9)
- **NATS E2E tests** — two MCP servers sharing a local NATS relay covering
  presence, messaging, and lifecycle (#18)
- **Hosted NATS tests** — same scenarios against Synadia Cloud or self-hosted
  NATS with weekly CI workflow (#20)
- **SDK tests** — Claude Agent SDK acceptance tests with real Claude
  sessions (#10)
- **Transcript capture** — `@pytest.mark.transcript` auto-saves human-readable
  transcripts to `tests/transcripts/`

### Fixed

- **Notification delivery** — fire `tools/list_changed` when description
  mutates so Claude Code picks up unread count changes (#21)
- **MCP config path** — use `~/.claude.json` (not `~/.claude/mcp.json`) for
  global MCP server registration (#25)
- **MCP server entry** — include required `type` field in server config (#26)
- **jq null guard** — `get_github_identity` filters null `.login` before
  processing (#31)

### Changed

- **Command vocabulary** — renamed tools to match BSD names: `biff` → `mesg`,
  `send_message` → `write`, `check_messages` → `read_messages` (#6, #12, #30)
- **CI** — added pyright to lint workflow (#28)
