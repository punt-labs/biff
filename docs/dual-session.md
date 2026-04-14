# Design: Dual-Session Biff (Ethos Roster)

**Bead:** biff-plqr
**Date:** 2026-04-14
**Status:** PROPOSED
**Related:** DES-002 (session key format), DES-009 (identity),
DES-030 (multi-agent coordination), DES-035 (TTY name reservation),
ethos-integration.md (identity + team resolution)

## Problem

One MCP server process = one session = one identity. But a Claude Code
session has two actors: the human (e.g. `jfreeman`) and the agent (e.g.
`claude`). Today, ethos resolves the *primary* identity -- whichever
fires first in the `whoami` chain -- and the other actor is invisible.

When `ethos whoami` resolves to `claude`, the human disappears from
`/who`. When it resolves to `jfreeman`, the agent disappears. Neither
is addressable via `/write`. Neither has a separate plan, idle time,
or kind tag.

This spec adds a second session registration within the same MCP server
process, driven by the ethos session roster.

## Non-Goals

- Process tree model (v2 design doc, Variant 1). That is future work
  for subagents, agent teams, and headless sessions.
- Separate inboxes per actor. Both actors share the MCP server; the
  model reads both inboxes.
- Interactive human tool calls. In Claude Code, the model is always the
  caller. The human does not invoke MCP tools directly.

## Ethos Session Roster

The ethos CLI provides session roster data. The expected call:

```bash
ethos session roster --json
```

Expected output:

```json
{
  "root": {
    "handle": "jfreeman",
    "kind": "human",
    "display_name": "Jim Freeman"
  },
  "primary": {
    "handle": "claude",
    "kind": "agent",
    "display_name": "Claude Agento"
  }
}
```

Fields:

| Field | Meaning |
|-------|---------|
| `root` | The human who launched the session. May be absent in headless. |
| `primary` | The main agent driving the session. May be absent for CLI-only use. |

When `root` and `primary` are different identities, biff registers
two sessions.

### Fallback

When `ethos session roster --json` fails (binary absent, exit 1,
timeout, malformed JSON, or `root`/`primary` are the same identity),
biff falls back to single-session behavior. The existing
`get_ethos_identity()` / `get_github_identity()` / `get_os_user()`
chain is unchanged. Zero visible difference for users without ethos.

## 1. Session Key Scheme

### Current

One session key per MCP server:

```text
{user}:{tty}
```

Where `user` comes from identity resolution and `tty` is an 8-char
random hex generated at startup.

### New

Two session keys per MCP server when dual-session is active:

```text
{primary_user}:{tty_primary}    # e.g. claude:a1b2c3d4
{root_user}:{tty_root}          # e.g. jfreeman:e5f6g7h8
```

Each identity gets its own TTY hex (two calls to `generate_tty()`).
Each gets its own TTY name reservation (two calls to
`claim_tty_name()`). They do NOT share a TTY -- sharing would violate
the DES-002 invariant that `{user}:{tty}` is a unique session
identifier, and would break DES-035 atomic name reservation (one name
per session key).

### Identity Assignment

The **primary** identity is the one that `ethos whoami` resolves to.
This is the identity that owns the MCP server's tool calls -- when
the model calls `/plan`, `/write`, `/wall`, the `from_user` is the
primary identity.

The **root** identity is the other actor from the roster. It is
registered for presence and addressability but does not originate
tool calls.

## 2. Roster Resolution

### New Function

```python
@dataclass(frozen=True)
class EthosRoster:
    """Session roster from ``ethos session roster --json``."""
    root: EthosIdentity | None
    primary: EthosIdentity | None

def get_ethos_roster() -> EthosRoster | None:
    """Resolve the session roster from ethos CLI.

    Returns ``None`` when ethos is not installed, not configured,
    returns malformed JSON, or times out.
    """
```

### Subprocess Call

```python
subprocess.run(
    ["ethos", "session", "roster", "--json"],
    capture_output=True,
    text=True,
    check=False,
    timeout=2,
)
```

Same pattern as `get_ethos_identity()`: 2-second timeout, silent
fallback on any error.

### Call Site

In `load_config()`, after identity resolution:

```python
# After resolving primary identity (user, display_name, kind):
roster = get_ethos_roster()
root_identity: EthosIdentity | None = None
if roster is not None and roster.root is not None:
    if roster.root.handle != user:
        root_identity = roster.root
```

`root_identity` is passed through `ResolvedConfig` to `create_state()`.

### Dual-Session Condition

Dual-session activates when ALL of:

1. `get_ethos_roster()` returns non-None.
2. `roster.root` is present.
3. `roster.primary` is present.
4. `root.handle != primary.handle` (different identities).

If any condition fails: single session, existing behavior.

## 3. ServerState Changes

### Current

```python
@dataclass(frozen=True)
class ServerState:
    config: BiffConfig        # primary identity
    relay: Relay
    tty: str                  # single TTY hex
    # ...

    @property
    def session_key(self) -> str:
        return build_session_key(self.config.user, self.tty)
```

### New

Add a companion identity for the root session:

```python
@dataclass(frozen=True)
class CompanionSession:
    """The secondary (root) identity in a dual-session setup."""
    user: str
    display_name: str
    kind: str
    tty: str                  # separate hex
    tty_name: str = ""        # set after claim

    @property
    def session_key(self) -> str:
        return build_session_key(self.user, self.tty)

@dataclass(frozen=True)
class ServerState:
    config: BiffConfig        # primary identity (unchanged)
    relay: Relay
    tty: str                  # primary TTY hex
    companion: CompanionSession | None = None  # NEW
    # ... rest unchanged

    @property
    def session_key(self) -> str:
        return build_session_key(self.config.user, self.tty)

    @property
    def companion_session_key(self) -> str | None:
        return self.companion.session_key if self.companion else None
```

### Why Not Two Configs?

The MCP server has one identity for tool calls. Adding a second
`BiffConfig` would create ambiguity about which config drives tool
handlers. The `companion` field cleanly separates "registered for
presence" from "owns tool calls."

## 4. Message Routing

### Delivery

Messages are delivered to user+tty inboxes via NATS subjects:

```text
biff.{repo}.inbox.{user}.{tty}
```

With dual-session, two subjects exist:

```text
biff.punt-labs__biff.inbox.claude.a1b2c3d4      # primary
biff.punt-labs__biff.inbox.jfreeman.e5f6g7h8    # companion
```

External senders use `/write @jfreeman` or `/write @claude` --
the relay routes to the correct subject based on the target user
and tty.

### Reading Both Inboxes

The MCP server's `_active_tick` checks both inboxes for unread
counts. The `read_messages` tool reads messages from the primary
inbox. Messages sent to the companion (root) inbox are also visible
-- see section 6 for polling changes.

### Who Reads What

| Sender | Target | Delivery subject | Who sees it |
|--------|--------|------------------|-------------|
| eric | `@claude` | primary inbox | Model via `/read` |
| eric | `@jfreeman` | companion inbox | Status bar + model via `/read` |
| eric | `@jfreeman:tty3` | companion inbox (targeted) | Same |

Both inboxes feed into the same MCP server. The model sees both.
This is correct: in Claude Code, the model mediates all
communication. The human sees messages via the status bar; the
model processes them via tool calls.

## 5. Tool Handler Identity

### The Invariant

All tool calls originate from the model. The model IS the primary
identity. Therefore:

- `/plan "working on X"` sets the primary's plan.
- `/write @eric "done"` sends as the primary user.
- `/wall "deploying"` posts as the primary user.

The companion (root) session is presence-only. It does not originate
tool calls. Its plan can be set via a dedicated tool:

```python
async def set_companion_plan(state: ServerState, message: str) -> str:
    """Set the plan for the human (companion) session."""
    if state.companion is None:
        return "No companion session active."
    # Update companion session in relay
    ...
```

This tool is optional and can be deferred. The primary use case
is the model setting the human's status on their behalf (e.g.,
"away" or "reviewing PR").

### No Caller Disambiguation Needed

The open question from v1 ("how does the tool handler know if the
human or the agent invoked it?") is resolved: the model is always
the caller. The human does not call MCP tools. The companion session
exists for presence and addressability, not for tool invocation.

## 6. Polling and `_active_tick`

### Current

One tick loop checks one inbox:

```python
summary = await state.relay.get_unread_summary(state.session_key)
```

### New

Check both inboxes:

```python
primary_summary = await state.relay.get_unread_summary(
    state.session_key
)
companion_summary = (
    await state.relay.get_unread_summary(state.companion_session_key)
    if state.companion_session_key
    else UnreadSummary()
)
total = primary_summary.count + companion_summary.count
```

The `read_messages` tool description shows the combined count:
`"Check messages (3 unread). Marks all as read."`

The `read_messages` handler reads from both inboxes and merges
results chronologically. Messages carry `to_user` so the display
can indicate the target:

```text
eric       10:31  @claude: test suite passed     [agent]
eric       10:28  @jfreeman: lunch?              [human]
```

### Display Queue

Wall and talk items are unchanged -- they are repo-scoped, not
session-scoped. The display queue tracks the combined unread count
from both inboxes.

### `tools/list_changed`

Fires when the combined count changes. One notification covers both
inboxes because there is one MCP session (one tool list).

## 7. Heartbeat and Cleanup

### Heartbeat

The `_heartbeat_loop` sends heartbeats for both sessions:

```python
await state.relay.heartbeat(state.session_key)
if state.companion_session_key:
    await state.relay.heartbeat(state.companion_session_key)
```

One loop, two heartbeat calls per interval. The companion session
must not expire while the MCP server is alive.

### Login/Logout Events

Both sessions get login events at startup:

```python
await _append_login_event(state, final_name)           # primary
await _append_companion_login_event(state)              # companion
```

Both get logout events on shutdown:

```python
await _append_logout_event(state)                       # primary
await _append_companion_logout_event(state)             # companion
```

### Session Cleanup

On shutdown, both sessions are deleted from KV and both TTY name
reservations are released:

```python
# In _release_relay():
# Release primary
await state.relay.release_tty_name(state.config.user, primary_name)
await state.relay.delete_session(state.session_key)
# Release companion
if state.companion:
    await state.relay.release_tty_name(
        state.companion.user, state.companion.tty_name
    )
    await state.relay.delete_session(state.companion.session_key)
```

### Signal Handler

The signal handler writes sentinel files for both session keys:

```python
_write_sentinel(state.config.repo_name, state.session_key)
if state.companion:
    _write_sentinel(state.config.repo_name, state.companion.session_key)
```

### Active Session Files

Both sessions get active-session markers:

```python
write_active_session(repo_name, state.session_key, worktree_root)
if state.companion:
    write_active_session(
        repo_name, state.companion.session_key, worktree_root
    )
```

## 8. `/who` Output

Both sessions appear as separate rows:

```text
NAME             K    REPO              IDLE  S  P  HOST
jfreeman:tty3         punt-labs/biff    0m    +  -  okinos
claude:tty4      [A]  punt-labs/biff    0s    +  +  okinos
```

The human shows no kind tag (default). The agent shows `[A]`. Both
are independently addressable:

- `/write @jfreeman` -- delivers to the companion inbox.
- `/write @claude` -- delivers to the primary inbox.
- `/write @jfreeman:tty3` -- targeted to the companion session.

The companion's idle time reflects the last heartbeat (always
current while the MCP server is running). If future work adds
human-activity detection (keyboard events, focus tracking), the
companion's `last_active` could diverge from the primary's.

## 9. Fallback Behavior

| Condition | Result |
|-----------|--------|
| Ethos absent | Single session, existing behavior |
| `ethos session roster --json` fails | Single session |
| Roster has no `root` | Single session |
| Roster has no `primary` | Single session |
| `root.handle == primary.handle` | Single session |
| Roster has both, different handles | Dual session |

The fallback path has zero new code paths executed. The `companion`
field is `None` and every dual-session code path is guarded by
`if state.companion`.

## 10. Edge Cases

### 10.1 Ethos Roster Changes Mid-Session

Biff reads the roster once at startup. Mid-session roster changes
(agent leaves, new agent joins) are not detected. This is consistent
with `get_ethos_identity()` which also runs once at startup.

Future work: a roster-watch mechanism could re-read the roster
periodically or on signal, adding/removing companion sessions
dynamically. This is out of scope for the initial implementation.

### 10.2 Human Opens a Second Claude Code Session

Each Claude Code session spawns its own MCP server process. Each
process reads the roster independently and registers its own pair
of sessions. The result:

```text
NAME             K    REPO              IDLE  S  P  HOST
jfreeman:tty3         punt-labs/biff    0m    +  -  okinos
claude:tty4      [A]  punt-labs/biff    0s    +  +  okinos
jfreeman:tty5         punt-labs/biff    0m    +  -  okinos
claude:tty6      [A]  punt-labs/biff    0s    +  +  okinos
```

Four sessions -- two pairs. `/write @jfreeman` broadcasts to both
`jfreeman` sessions (tty3 and tty5). `/write @jfreeman:tty3`
targets one. This is consistent with the existing multi-session
model (DES-002).

### 10.3 Sub-Agent Spawned via Agent Tool

Sub-agents (Agent tool, TeamCreate) run as separate Claude Code
processes. If the sub-agent's process has its own MCP server (which
it does -- Claude Code spawns MCP servers per session), it registers
its own sessions via the same roster mechanism. The sub-agent sees
the same roster (same repo, same ethos config) and registers its own
pair.

This is correct: the sub-agent IS a separate session. It should be
visible in `/who` and addressable independently. The parent-child
relationship is not modeled by biff today (that is the process tree
model from v2, future work).

### 10.4 Headless Session (SDK, CI)

The roster may have `primary` but no `root` (no human). This
triggers single-session mode (condition: `root` must be present for
dual-session). The agent registers as the sole session. Correct
behavior: there is no human to register.

### 10.5 CLI-Only Session (No Agent)

The roster may have `root` but no `primary` (human using `biff`
CLI directly, no Claude). This triggers single-session mode. The
human registers as the sole session. Correct behavior: there is no
agent to register.

### 10.6 Companion Session Heartbeat Failure

If a heartbeat for the companion fails (transient NATS error), the
primary heartbeat loop logs a warning and continues. The companion
session may expire in KV if heartbeats fail for the full TTL
duration. On the next successful heartbeat, the companion session
is re-registered. This matches the existing heartbeat resilience
model.

### 10.7 KV Watcher Sees Companion Delete

The `_handle_kv_delete` function skips `state.session_key` to avoid
writing duplicate logout events. It must also skip
`state.companion_session_key` for the same reason. Both keys are
managed locally; their logout events are written explicitly during
graceful shutdown.

## 11. Implementation Sequence

1. **`config.py`**: Add `get_ethos_roster()`, `EthosRoster` dataclass.
   Extend `ResolvedConfig` with `root_identity: EthosIdentity | None`.
2. **`state.py`**: Add `CompanionSession` dataclass. Add `companion`
   field to `ServerState`. Extend `create_state()` to accept and wire
   the companion.
3. **`app.py` lifespan**: Register companion session after primary.
   Claim companion TTY name. Write companion login event.
4. **`app.py` heartbeat**: Send heartbeats for both sessions.
5. **`app.py` cleanup**: Release both TTY names, delete both sessions,
   write both logout events, write both sentinel files.
6. **`_descriptions.py`**: Poll both inboxes in `_active_tick`.
   Combine unread counts.
7. **`read_messages` tool**: Read from both inboxes, merge
   chronologically.
8. **`_session.py`**: Add `update_companion_session()` helper.
9. **Tests**: Unit tests for roster resolution, integration tests for
   dual-session lifecycle, presence tests for `/who` output with two
   sessions.

## 12. Files Changed

| File | Change |
|------|--------|
| `src/biff/config.py` | Add `EthosRoster`, `get_ethos_roster()`. Extend `ResolvedConfig`. |
| `src/biff/models.py` | No changes (existing `kind` field suffices). |
| `src/biff/server/state.py` | Add `CompanionSession`. Add `companion` field to `ServerState`. |
| `src/biff/server/app.py` | Dual registration, dual heartbeat, dual cleanup, dual sentinel. |
| `src/biff/server/tools/_descriptions.py` | Dual inbox polling in `_active_tick`. Combined unread. |
| `src/biff/server/tools/_session.py` | Add `update_companion_session()`. |
| `src/biff/server/tools/read_messages.py` | Read from both inboxes. |
| `src/biff/tty.py` | No changes (called twice, once per identity). |
| `tests/test_server/test_config.py` | Roster resolution tests. |
| `tests/test_integration/` | Dual-session lifecycle tests. |

## 13. Startup Cost

| Call | Expected latency | Timeout |
|------|-----------------|---------|
| `ethos session roster --json` | ~12ms | 2s |
| `ethos whoami --json` (existing) | ~10ms | 2s |
| `ethos team for-repo --json` (existing) | ~15ms | 2s |

The roster call adds ~12ms to startup when ethos is installed. When
ethos is absent, the `FileNotFoundError` adds ~1ms. When the roster
indicates single-session (same identity or missing field), no
additional work is done beyond parsing the JSON.

Second TTY name claim adds one NATS KV `create()` round-trip (~5ms
on demo relay).
