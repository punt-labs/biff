# Changelog

## Unreleased

### Fixed

- **plan_source priority enforcement** — auto plans (from git hooks) can no longer
  overwrite manual `/plan` entries. The guard was documented but not implemented;
  both Copilot and Cursor caught this independently.
- **SessionEnd repo_name mismatch** — `handle_session_end()` now uses the same
  sanitized repo slug as `write_active_session()` (e.g. `punt-labs__biff`), fixing
  a comparison that silently prevented session cleanup when a git remote was
  configured.
- **Branch regex false positives** — `_BEAD_BRANCH_RE` now uses word boundaries
  (`\b`), preventing common branch names like `my-feature` from being truncated
  to `my-feat` and misidentified as bead IDs.

## 0.8.0 — 2026-02-24

### Added

- **`biff hook` CLI dispatcher** (DES-017) — new `biff hook claude-code <event>`
  and `biff hook git <event>` command groups. All hook logic moves from shell
  scripts to versioned Python. Shell scripts become thin dispatchers with a
  fast `.biff` file-existence gate. (#biff-7vp)
- **Plan auto-expand** — `/plan biff-ka4` now resolves the bead title via
  `bd show --json -q` and expands to `biff-ka4: post-checkout hook`. Falls
  back to the raw string if `bd` is unavailable or the ID is invalid. (#biff-5zq)
- **`plan_source` field** — `UserSession` now tracks how the plan was set
  (`"manual"` or `"auto"`). Manual `/plan` calls always set `"manual"`.
  Git hooks (Phase 2) will set `"auto"` and only overwrite auto plans,
  preventing automated hooks from clobbering intentional plans. (#biff-efk)
- **SessionStart hooks** — on startup, nudges Claude to auto-assign `/tty`,
  set `/plan` from the current git branch (with bead ID expansion), and
  check `/read` for unread messages. On resume/compact, re-orients Claude
  with a `/read` reminder. Branch-inferred plans use `source="auto"` so
  git hooks can later overwrite them. (#biff-6we)
- **SessionEnd cleanup** — on session end, converts active session markers
  (`~/.biff/active/`) to sentinel files for the existing reaper. MCP server
  writes active markers on startup; the hook converts them to sentinels before
  potential SIGKILL, ensuring session presence is cleaned up even on abrupt
  termination. (#biff-w5c)
- **Git post-checkout hook** — on branch switch, writes a plan hint file
  (`~/.biff/plan-hint`) with the expanded branch name (including bead ID
  resolution). The PostToolUse Bash handler picks up the hint and nudges
  Claude to set the plan with `source="auto"`. Switching to main/master
  clears the plan. (#biff-ka4)
- **Git post-commit hook** — after each commit, writes a plan hint with
  `✓ <subject>` so teammates see commit progress in `/finger` and `/who`.
  Uses the same plan hint file mechanism as post-checkout. (#biff-crz)
- **Git pre-push hook** — when pushing to main/master, writes a wall hint
  file (`~/.biff/wall-hint`). The PostToolUse Bash handler picks up the
  hint and suggests `/wall <summary>`. Silent for feature branch pushes.
  (#biff-9e7)
- **Git hook deployment** — `biff enable` deploys post-checkout, post-commit,
  and pre-push hooks into `.git/hooks/`. `biff disable` removes them. Hooks
  coexist with existing git hooks (e.g. beads post-merge) via marked blocks.
  `biff doctor` reports missing hooks. (#biff-9z2)

### Changed

- **Migrated bead-claim and PR-announce hooks** — `bead-claim.sh` (55 lines)
  and `pr-announce.sh` (55 lines) replaced by `post-bash.sh` and
  `pr-announce.sh` thin dispatchers (4 lines each) plus Python handlers
  in `hook.py`. (#biff-7vp)

## 0.7.0 — 2026-02-24

### Changed

- **Shared NATS streams** — consolidated per-repo streams into 3 shared streams
  (`biff-inbox`, `biff-sessions`, `biff-wtmp`) with subject-based repo isolation.
  Removes the 8-repo limit imposed by Synadia Cloud's 25-stream cap. (#62, #64)
- **Idempotent stream provisioning** — create-or-update replaces
  delete-and-recreate, preventing accidental data loss in shared streams (#62)
- **Scoped purge** — `purge_data()` uses subject filters to purge only the
  current repo's data, not the entire shared stream (#62)

### Added

- **Wtmp schema versioning** — `SessionEvent.version` field enables
  forward-compatible schema evolution for the 30-day retention wtmp stream (#64)
- **Stream namespace isolation** — `stream_prefix` parameter on `NatsRelay`
  separates test streams (`biff-dev-*`) from production (`biff-*`) (#64)
- **Encryption extension points** — reserved KV key namespaces (`key.*`,
  `team-key`) and model fields (`UserSession.public_key`, `Message` encryption
  envelope) for future E2E encryption (biff-lff). No encryption code yet. (#62)

### Fixed

- **Resilient consumer cleanup** — `delete_session()` suppresses `TimeoutError`
  and `NatsError` during consumer deletion; `inactive_threshold` is the safety
  net (#64)
- **Legacy stream cleanup** — startup migration deletes orphaned per-repo streams
  with error suppression to avoid crash on first boot after upgrade (#63)

## 0.6.0 — 2026-02-23

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
- **Team broadcast** — `/wall` command for time-limited team announcements modeled
  after BSD `wall(1)`. Posts a banner visible on every teammate's status bar and
  tool descriptions. Duration-based expiry (default 1h, max 3d), lazy expiry on
  read, three modes: post, read, clear (#biff-klz)
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
