# Changelog

## [0.16.0] - 2026-03-08

### Added

- **Interactive REPL** — `biff` with no args launches a persistent interactive
  session with full BSD command vocabulary. Unified session lifecycle (connect,
  register in KV, auto-assign ttyN, wtmp login/logout, clean shutdown) replaces
  the ephemeral 5-minute TTL session hack. All CLI modes (REPL, inline, talk)
  share one `cli_session()` context manager. (#biff-vrk)
- **REPL readline** — line editing (arrow keys, Home/End, Ctrl-A/E), command
  history persisted to `~/.biff/repl_history`, and tab completion for command
  names. Detects libedit (macOS) vs GNU readline for correct binding syntax.
  (#biff-x6s)
- **REPL inline notifications** — real-time NATS-driven message alerts and wall
  broadcasts while idle at the prompt. Self-notification prevention via state
  sync. Prompt gate prevents output/prompt interleave. Session prompt shows
  `user:ttyN>`. (#biff-02y)
- **REPL talk mode** — `talk @user` enters modal conversation mode (like BSD
  talk). Typed lines deliver to target, `end` returns to REPL. Incoming messages
  display as banners via NATS notification queue. Talk invitation includes tty
  for easy reply. (#biff-q07)
- **Two-phase talk handshake** — BSD-style talk protocol: inviter waits for
  accept before entering talk mode, responder detects pending invite via
  persistent set, mutual hangup ends both sides. Talk messages use NATS core
  publish (no inbox pollution). Cyan color for incoming, `user:tty >` prompt
  style. (#biff-1di)
- **Z specifications** — formal Z specs for talk handshake (`docs/talk.tex`,
  565K states) and REPL state machine (`docs/repl.tex`, 509K states), both
  verified with ProB (no counter-examples). Makefile targets for fuzz
  type-checking and ProB animation/model-checking. (#biff-1di)
- **CLI multi-user test tier** — tier 2b integration tests: two CLI users
  sharing a local NATS relay exercise who, finger, write, read, wall, plan,
  talk, and last via `cli_session`. 102 new tests total across talk partition
  tests, REPL loop control flow, and NotifyState boundary tests. (#biff-s8d,
  #biff-1di)
- **Session identity model** — Z specification (`docs/session-model.tex`)
  modeling the full organizational hierarchy from orgs to processes, with design
  documents exploring dual identity for human+agent sessions.
- **Wall broadcasts voiced via vox** — when vox is installed, wall messages are
  spoken aloud with emoticon-to-vibe mapping (`:D` becomes excited, `!!` becomes
  urgent). L0/L1 peer integration: graceful degradation when vox is absent.
  (#biff-a4d)

### Fixed

- **PostToolUse hook matches standalone MCP tool names** — the matcher
  `mcp__plugin_biff(-dev)?_tty__.*` only matched plugin-namespaced tools.
  Standalone MCP servers use `mcp__tty__*` which didn't match. Fix:
  `mcp__(plugin_biff(-dev)?_)?tty__.*` matches both patterns. (#biff-hv5)
- **TTY name race condition** — two sessions starting simultaneously could both
  compute the same ttyN name (TOCTOU between get_sessions and update_session).
  Fix: two-phase write-then-verify approach. `assign_unique_tty_name()` computes
  the name, caller writes to KV, `verify_tty_name()` re-reads and reassigns on
  collision. (#biff-1rn)
- **suppress-output.sh counts data rows not wrapped lines** — `wc -l` inflated
  row counts due to 80-char line wrapping. Each handler now uses format-specific
  grep patterns matching actual output rows. (#biff-1hn)

### Changed

- **Makefile gains Z spec targets** — `make fuzz SPEC=...` for type-checking,
  `make prob SPEC=...` for ProB animation and model-checking,
  `make prob-session` for the session identity model. `clean-tex` covers
  `docs/` artifacts.

## [0.15.1] - 2026-03-06

### Fixed

- Show help when `biff` is invoked with no command
- Fire-and-forget MCP tools no longer block on NATS publish; argv hoisting removed
- Test suite respects TMPDIR for config isolation

### Changed

- Add Makefile per makefile.md standard

## [0.15.0] - 2026-03-06

### Added

- **CLI parity with MCP tools** — every MCP product tool is now available as `biff <command>`:
  `who`, `finger`, `write`, `read`, `plan`, `last`, `wall`, `mesg`, `tty`, `status`
- **`--json` global flag** — machine-readable JSON output on all CLI commands
- **`biff status`** — connection state, active session, unread count, wall posts
- **Library API** — `from biff import commands, CliContext, CommandResult` for programmatic use;
  each CLI command is a pure async function that takes context + args and returns `CommandResult`
- **Testable command functions** — `biff.commands` module with 10 pure async functions
  (`who`, `finger`, `write`, `read`, `plan`, `last`, `wall`, `mesg`, `tty`, `status`);
  callable directly with `LocalRelay` for unit testing without NATS
- **Shared formatting module** — `biff.formatting` extracts domain-level format functions
  from MCP tool closures for reuse by both CLI and MCP surfaces
- **CLI session manager** — pseudo-ephemeral NATS sessions with 5-minute TTL for
  consecutive CLI commands (`biff.cli_session`)

### Fixed

- **Notification deferral** — NATS talk callbacks and KV wall watchers no longer fire MCP
  notifications directly (unreliable from background coroutine contexts). Both now wake the
  poller via `ActivityTracker.wake()`, which resets `_last_nap_poll` to epoch so `_active_tick`
  runs on the next 2s tick even during napping. Worst-case notification latency: ≤2s.
- **KV watcher survives snapshot-done** — `_run_kv_watch` uses explicit `updates()` loop
  instead of `async for`, surviving `None` snapshot-done markers and `TimeoutError` without
  restarting the watcher
- Suppress asyncio `eof_received` warning on NATS SSL disconnect
- Suppress nats.py deprecation warnings and disconnect noise in CLI
- Standards compliance — CLI flags, hooks, commands, suppress-output

### Changed

- CLI product commands now delegate to `biff.commands` pure async functions via a `_run()` adapter,
  replacing inline `_*_async` implementations with one-liner delegations
- `CliContext.relay` widened from `NatsRelay` to `Relay` protocol, enabling `LocalRelay` in tests
- Moved primitive formatting layer from `biff.server.tools._formatting` to `biff._formatting`
  to break circular import between `biff.formatting` and the server tools package
- Adopt dev/prod plugin namespace isolation: `plugin.json` name is `"biff-dev"` on main,
  release scripts swap to `"biff"` on tagged commits only. Dev commands (`*-dev.md`) route
  to `mcp__plugin_biff-dev_tty__*` to avoid collisions with the installed production plugin.
- Z specification amended: `KVWallReceive` and `NatsTalkCallback` defer notification to
  `PollTick`; formally verified with ProB (550K+ states, no counter-examples)

## [0.13.0] - 2026-03-02

### Fixed

- Talk push notifications from CLI now reach MCP session — `_session` is eagerly captured during `initialize` via `SessionCaptureMiddleware`, fixing the suspenders notification path for NATS callbacks (biff-8g0)

### Changed

- Status line shows dim `/biff y to enable team communication` hint when biff is not enabled for the repo, replacing the unhelpful bare `biff` label

## [0.12.2] - 2026-02-28

### Fixed

- `gh` CLI check in `biff doctor` is now optional — users without `gh auth login` no longer see a required failure or a non-zero exit code
- Installer uses `doctor || true` so diagnostic failures don't abort the install script under `set -eu`

## [0.12.1] - 2026-02-28

### Fixed

- Installer auto-installs Python 3.13 via `uv python install` when system Python is too old (Ubuntu 24.04 ships 3.12)
- Installer checks for git before marketplace operations, failing fast with a clear message instead of opaque errors
- Installer uses uninstall-before-install for idempotency (`claude plugin update` is unreliable)
- Installer adds read-after-write verification after plugin install

## [0.12.0] - 2026-02-27

### Added

- **SessionStart collision detection** — when multiple Claude Code sessions are
  active in the same worktree, the SessionStart hook emits an advisory suggesting
  `/who` and worktree usage to avoid conflicts. Active session files now include
  worktree root for precise matching.

### Fixed

- Installer now refreshes marketplace clone before plugin install, ensuring existing users get the correct `source.ref` pins

## [0.11.4] - 2026-02-26

### Fixed

- **Installer now installs from PyPI** — `install.sh` installs the released
  `punt-biff` package instead of building from git source. Faster installs,
  tested artifacts.
- **Re-running installer upgrades the plugin** — `biff install` now calls
  `claude plugin update` when the plugin is already installed, so existing
  users get new versions without manual intervention.
- **SSH-less users can install** — `install.sh` detects missing SSH keys and
  temporarily rewrites git URLs to HTTPS for `claude plugin install`.
- **Install failures show error messages** — `biff install` and `biff doctor`
  are now wrapped in `if !` guards so `set -eu` doesn't cause silent exits.

## [0.11.3] - 2026-02-26

### Fixed

- **MCP server launch no longer requires `uv`** — production `plugin.json`
  now invokes `biff serve` directly instead of `uv run biff serve`. Users
  who install via `uv tool install punt-biff` have the binary on their PATH;
  the `uv run` wrapper was an unnecessary dependency that could also pick up
  wrong project environments.

## [0.11.2] - 2026-02-26

### Fixed

- **Dev commands no longer ship to marketplace users** — moved `*-dev.md`
  commands from `commands/` (plugin-shipped) to `.claude/commands/`
  (project-local). Dev commands now only load when working in the biff repo.

## [0.11.1] - 2026-02-26

### Fixed

- **Wall messages now actually rotate** — each wall post gets a unique source
  key (`wall:{posted_at}`) so multiple walls accumulate in the display queue
  instead of replacing each other. Old walls expire naturally based on their
  original duration.
- **Statusline reads all display items** — unread file now contains a
  `display_items` array (replacing single `display_text`/`display_kind`).
  The statusline does time-based rotation (`int(time / 15) % n`) — stateless,
  deterministic, no persisted index needed.

### Added

- **`DisplayItem.expires_at`** — optional monotonic timestamp for automatic
  expiry. Expired items are purged on `current()` and `advance_if_due()`.
- **`DisplayQueue.expires_from_now()`** — helper for computing monotonic expiry
  from wall-clock remaining seconds.

## [0.11.0] - 2026-02-26

### Added

- **Display queue for status bar rotation** — wall and talk items now rotate on
  status bar line 2 (15s per turn). Wall items cycle indefinitely until they
  expire or are cleared. Talk items show once then discard. Multiple wall
  broadcasts rotate so none are hidden. (#biff-j8b)

### Changed

- **Talk messages coalesce per sender** — rapid messages from the same sender
  replace the previous queue item instead of growing the queue without bound.
- **Talk queue clears on partner switch** — changing talk partners removes stale
  messages from the previous conversation immediately.
- **Unified unread file schema** — `display_items` array replaces
  separate `wall`/`wall_from`/`talk_partner`/`talk_message` fields.
- **Injected clock for DisplayQueue** — `clock` parameter (defaults to
  `time.monotonic`) enables deterministic testing without `time.sleep`.
- **Dev/prod namespace isolation** — plugin commands and MCP server names are
  namespaced for dev/prod isolation per punt-kit plugins.md standard. (#81)
- **Repository URL in project metadata** — `pyproject.toml` now includes
  `project.urls` per punt-kit standard. (#80)

## [0.10.6] - 2026-02-25

### Fixed

- **Talk status bar updates are now instant** — the `talk` tool description is now
  mutated when talk messages arrive (e.g. `[TALK] @sender: message...`), mirroring
  the wall pattern. Previously, `notify_tool_list_changed()` fired but Claude Code
  saw no tool description change and skipped the UI re-render. (DES-020)

### Changed

- **`refresh_talk()` added** — mirrors `refresh_wall()`. Mutates the talk tool
  description, fires `notify_tool_list_changed()`, and rewrites the unread file.
- **`_sync_talk_to_file` deleted** — replaced by `refresh_talk()` which handles
  both tool description mutation and file write.
- **`_notify_tool_list_changed` → `notify_tool_list_changed`** — made public
  since it is called from `refresh_talk()` and `refresh_wall()`.

## [0.10.5] - 2026-02-25

### Fixed

- **Status bar latency regression fixed** — wall and talk updates now arrive within
  0-2s on idle sessions instead of 2+ minutes. Root cause: nap mode disconnected the
  NATS TCP connection, killing all KV watches and subscriptions. Now nap mode keeps the
  connection alive and reduces polling frequency instead. (DES-019)

### Changed

- **KV watcher detects wall changes** — `_run_kv_watch` now routes wall key updates
  through `refresh_wall()` → `_notify_tool_list_changed()` for instant push notifications
  to Claude Code. Previously wall changes were only detected by the 2s poller.
- **Heartbeat fires during nap** — idle sessions maintain heartbeat on schedule
  regardless of nap state, preventing session liveness gaps.
- **POP-mode connection cycling eliminated** — `_pop_fetch()` removed, `disconnect()`
  no longer called during nap. NATS connection persists for the full server lifetime.

## [0.10.4] - 2026-02-25

### Fixed

- **Talk push notifications are immediate** — incoming talk messages now trigger
  `_sync_talk_to_file()` and `_notify_tool_list_changed()` directly in the NATS
  callback instead of waiting for the 2s poller tick. Status bar updates appear
  within 0-2s instead of 4-6s (or never, if the poller was napping).

### Changed

- **Release process updated in CLAUDE.md** — both channels (marketplace + PyPI)
  now ship together on every version bump. Removed "milestone only" PyPI policy.
  Documented that local editable installs must never be used and that `twine upload`
  must never be run manually.

## [0.10.3] - 2026-02-25

### Fixed

- **Heartbeat no longer overwrites session metadata** — the heartbeat error path
  created a bare `UserSession(user, tty)` when KV reads failed, destroying
  `tty_name`, `plan`, `hostname`, and other fields. On hosted NATS, transient
  network issues triggered this regularly, wiping session identity. Now heartbeat
  skips missing or corrupt sessions instead of overwriting them.
- **Talk resolves tty_name before delivery** — `/talk @user:tty1` now maps
  friendly tty names to hex session keys via `resolve_session` before message
  delivery. Without this, messages addressed to tty_name failed to route.
- **Validate sender_key in deliver()** — `_validated_sender_key()` checks format
  (`user:tty`) and user-part consistency before embedding in talk notifications.
  Invalid keys are silently dropped.

### Changed

- **POP interval lowered to 10s** — idle sessions now check for messages every
  10 seconds (was 10 minutes). Idle threshold restored to 120s (was 30s).
  Status bar updates are no longer delayed by minutes during nap cycles.

## [0.10.2] - 2026-02-25

### Fixed

- **Talk self-echo on status bar** — when both sides of a `/talk` are the same
  user (different ttys), outgoing messages echoed on the sender's own status bar.
  Notification payload now includes `from_key` (sender session key) so the
  callback rejects notifications from the current session.
- **talk_listen no longer encourages loop** — updated tool description to say
  "agent-to-agent only" and "human sessions should NOT call this." The old
  description actively encouraged `talk_listen` loops, overriding the `/talk`
  command's status-bar auto-read instructions.

## [0.10.1] - 2026-02-25

### Fixed

- **Talk honors `:tty` address targeting** — `/talk @user:tty` was parsing the
  address but discarding the tty, delivering messages to the user-level inbox
  instead of the targeted session. Now `set_talk_partner` stores the full address,
  `deliver()` targets the specific tty, and the notification filter extracts the
  user-part for comparison.

## [0.10.0] - 2026-02-25

### Changed

- **Talk v2: status-line auto-read** — `/talk` now displays incoming messages on
  the status bar within 0-2s instead of blocking on `talk_listen`. The background
  poller subscribes to NATS core pub/sub notifications and writes talk messages to
  the unread status file. Line 2 priority: talk (bold yellow) > wall (bold red) >
  idle. Both parties agree to `/talk`, then auto-read each other — no `/read`
  needed. (#biff-q97)
- **NATS talk notifications carry message body** — `_publish_talk_notification`
  now sends JSON with sender and body (was a bare `b"1"` wake signal). The poller
  NATS subscription captures message content for status bar display.
- **`/talk` command updated** — no more `talk_listen` loop. Incoming messages
  appear on the status bar automatically. Use `/write` to reply, `/talk end` to
  close.

## [0.9.1] - 2026-02-25

### Fixed

- **Missing `/talk` slash command** — added `talk.md` command file so `/talk`
  appears in the skills list and deploys via SessionStart hook. The MCP tools
  existed since v0.9.0 but the slash command was never created.
- **Uninstall cleanup** — added `talk.md` to `BIFF_COMMANDS` in installer so
  `biff uninstall` removes it from `~/.claude/commands/`.

## [0.9.0] - 2026-02-25

### Added

- **Real-time talk** — three new MCP tools (`/talk`, `/talk_listen`, `/talk_end`)
  for real-time bidirectional conversation between biff sessions. Supports
  human↔agent, human↔human, and agent↔agent conversations. Uses NATS core
  pub/sub for instant notification with subscribe-before-check pattern to
  prevent race conditions. (#biff-8t3)
- **`biff talk` CLI** — `biff talk @user [message]` command for terminal-based
  interactive conversations. Single persistent stdin reader thread, NATS
  notification-driven message display, online presence check before connecting.

## [0.8.2] - 2026-02-24

### Fixed

- **Wall tty in status bar** — `_wall_from` now includes the sender's tty name
  so the status bar shows it (was only in tool description after v0.8.1).
- **Redundant session fetch** — wall tool reuses `update_current_session` return
  value instead of calling `get_or_create_session` a second time.
- **README image on PyPI** — use absolute GitHub URL for `biff.png` so it renders
  on pypi.org (relative paths don't resolve there).

## [0.8.1] - 2026-02-24

### Fixed

- **Wall sender tty** — `/wall` now includes the sender's tty name (e.g.
  `@kai (main)`) in the wall output, tool description, and status bar. Previously
  only the username was shown. (#biff-nw9)

## [0.8.0] - 2026-02-24

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
- **Hint file session race** — plan and wall hint files are now scoped by git
  worktree path (`~/.biff/hints/{hash}/`). Multiple sessions in different
  worktrees no longer race on shared hint files. Sessions in the same worktree
  share hints by design — the coordination contract requires worktree isolation.
- **Hint content escaping** — branch names and commit subjects containing double
  quotes no longer break the `/plan with message="..."` prompt syntax. Content is
  now JSON-escaped before embedding.

## [0.7.0] - 2026-02-24

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

## [0.6.0] - 2026-02-23

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
