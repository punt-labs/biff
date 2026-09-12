# biff Harness Compatibility — opencode, pi, codex

Research synthesis, 2026-09-11. Three parallel agents investigated each
harness from its installed specimen, its source repository, and its shipped
documentation, carrying the Claude Code post-mortem as prior art: tool
description mutation does not reach the model mid-session there (pinned
prompt-cache tools snapshot, DES-038 amendment), and the working stack is
push-fed ground truth + per-event context injection + scheduled turns.

## The meta-finding

**Claude Code is the most constrained harness of the four.** Every other
harness offers an open primitive to push a turn or context into a running
(even idle) model:

| Harness | Push-into-model primitive | Idle wake? | Gated? |
|---|---|---|---|
| Claude Code | Channels (`notifications/claude/channel`) | yes | **org-gated, unusable** |
| opencode | `client.session.prompt` (plugin SDK) | yes | open |
| pi | `pi.sendMessage(..., {triggerTurn: true})` | yes | open |
| codex | `codex queue` / `thread/queue/add` (JSON-RPC, durable SQLite) | yes (~10s watcher) | open |

The capability Anthropic gates behind `channelsEnabled` ships open in all
three alternatives. pi's author even ships biff's exact receive pattern as
an example extension (`file-trigger.ts`: watch a file, inject its contents,
wake the model — "useful for external systems to send messages to the
agent").

## Description mutation, per harness

| Harness | Viable? | Mechanism |
|---|---|---|
| Claude Code | **No** | tools block is a session-start prompt-cache snapshot; client re-fetches on list_changed but never re-serializes into the conversation |
| opencode | **Yes** (works today, zero new code) | list_changed handler refreshes a live cache; tools re-serialized every agentic step (`mcp/index.ts:462`, `session/tools.ts:390`) |
| pi | Viable but pointless | tools re-serialized from live session state per request; direct injection is strictly better and mutation thrashes provider prompt cache |
| codex | **No** | per-server tool catalog fetched once at client startup; `on_tool_list_changed` is a logging no-op for user-configured servers (`rmcp_client.rs:969`, `logging_client_handler.rs:86`) |

Conclusion: description markers stay display metadata (DES-038 amendment
holds across harnesses); opencode gets them for free but should not depend
on them.

## Per-harness user story

### opencode — tier: PARITY-OR-BETTER

- **Install**: `uv tool install punt-biff` + one plugin (npm ref in
  `opencode.json` `"plugin"` or a deposited file in
  `~/.config/opencode/plugins/`) + an `mcp` config entry running `biff mcp`.
- **A teammate messages you**: the plugin's
  `experimental.chat.messages.transform` hook fires on every LLM request
  (including between tool calls) and appends a change-gated synthetic
  nudge — opencode's own native reminder pattern. Idle: the plugin holds a
  timer + `session.idle` event and calls `client.session.prompt` to wake
  the session. Human sees `tui.showToast`.
- **Commands**: existing MCP tools work unchanged; slash idiom via plugin.
- **Prerequisite**: session-identity remapping — an opencode server hosts
  N sessions per instance and MCP calls don't carry sessionID; the plugin
  (which has sessionID in every hook) must bridge to biff's session model.
- **Risks**: the two best hooks are `experimental.*`-prefixed; fast release
  cadence; `tui.*` verified for TUI only.

### pi — tier: PARITY-PLUS

- **Install**: `uv tool install punt-biff` + `pi install
  npm:@punt-labs/biff-pi` (a new TypeScript extension package). No MCP —
  pi deliberately has none; extensions are the idiom.
- **A teammate messages you**: the in-process extension watches biff's
  delivery feed from `session_start`; mid-task arrivals inject via
  `pi.sendMessage(..., {deliverAs: "steer"})` — the model reads them
  before its next LLM call. Idle: `triggerTurn: true` wakes the model
  outright, consent-gated (mesg semantics — wakes cost tokens; default:
  talk invites wake, ordinary messages queue as `nextTurn`). Human sees a
  persistent footer via `ctx.ui.setStatus` plus `ctx.ui.notify` toasts.
- **Commands**: full vocabulary as pi slash commands AND native
  model-callable tools (`pi.registerTool`) backed by `biff --json` CLI
  verbs — the model can reply autonomously after a wake.
- **Risks**: 0.x API churn (freshly acquired by Earendil; pin versions),
  unsandboxed extension trust, wake-loop discipline between two biff'd
  agents (change-gate + rate limit + mesg), ethos identity resolution from
  pi's process tree unverified.

### codex — tier: PARITY-PLUS on delivery, degraded on statusline

- **Install**: codex plugin bundling `[mcp_servers.biff]` + hooks (codex
  plugins carry both), or `biff install --codex` writing config +
  `~/.codex/hooks.json`.
- **A teammate messages you**: biff's daemon runs `codex queue --thread
  <session-name> --message "N unread from @x — call read_messages"` (or
  speaks `thread/queue/add` on the daemon socket). Delivered as a real,
  user-visible turn within ~10s even when idle — replaces both the nudge
  hook AND the cron. Belt: codex's hook engine is Claude-hooks-COMPATIBLE
  (literally `ClaudeHooksEngine`, same `hookSpecificOutput.additionalContext`
  JSON, even `CLAUDE_PLUGIN_ROOT`), so the existing unread-nudge hook ports
  near-verbatim to UserPromptSubmit.
- **Session identity**: SessionStart hook registers the thread; thread
  names are settable (`thread/name/set`) → `/tty` maps directly.
- **Commands**: MCP tools work as plain asks; codex custom prompts
  (`~/.codex/prompts`) are the slash idiom — biff ships prompt files
  mirroring `plugin/commands/`; an AGENTS.md fragment covers orientation.
- **Gap**: no scriptable statusline — the queued nudge is a visible
  user-turn line in the transcript (more honest than invisible injection,
  but no persistent glanceable badge); optional desktop notification.
- **Risks**: end-to-end queue delivery not yet live-verified (first canary:
  interactive session + `codex queue` from another shell); HEAD-vs-0.151.0
  drift; `thread/queue/add` requires an experimental client capability
  declaration; per-hash hook trust re-approval on updates.

## The universal architecture

One pattern fits all four harnesses:

1. **biff's Python/NATS core is unchanged everywhere** — relay, push
   detection, per-session unread state, CLI verbs.
2. **A stable delivery contract** feeds each harness adapter. Candidates:
   (a) the per-session unread-state file (already consumed by the Claude
   statusline + nudge hook and proposed for pi's fs.watch) — needs
   promotion to a documented public interface; or (b) a new `biff watch`
   long-running subcommand emitting JSON lines on message events — cleaner,
   harness-agnostic, and the recommended direction once a second harness
   ships.
3. **A thin per-harness edge adapter** (~300–500 LOC each) translates the
   feed into that harness's injection idiom: Claude Code hook (shipped),
   opencode plugin, pi extension, codex queue-worker + hook.
4. **Consent discipline everywhere**: change-gated nudges, mesg-style
   opt-in for idle wakes (they spend tokens), rate limiting against
   agent-to-agent ping-pong.

## Recommended sequencing

1. **pi first** — biggest capability win (true idle wake), smallest surface
   (one extension, no MCP), author-blessed pattern, and punt-labs uses pi
   daily.
2. **opencode second** — parity-or-better with modest work; resolve the
   session-identity mapping as the design gate.
3. **codex third** — parity via queue+hooks; start with the live
   `codex queue` canary before building.
4. **Claude Code Channels** remains the watch item (corporate-account test);
   if it opens, the same delivery contract feeds it as a fourth adapter.

Full per-harness detail with file:line evidence lives in the three research
agents' reports (session transcript, 2026-09-11); this document is the
decision-ready summary.
