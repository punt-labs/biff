# Spec: Agent-First Identity Resolution

**Bead:** biff-8fg3
**Date:** 2026-04-16
**Status:** PROPOSED
**Pipeline:** formal-2026-04-16-dc439e, stage 1 (spec)
**Replaces:** The implicit identity model accumulated across v1.8.0--v1.10.0

## 1. Identity Model

A biff MCP server operates with two identity slots:

**Primary (agent).** The MCP server's operating identity. Used for:

- Session registration in NATS KV (`config.user`)
- Outbound message FROM field (`/write`, `/wall`, `/talk`)
- Heartbeat session key
- Session key construction (`{user}:{tty}`)
- wtmp login/logout events

**Companion (human).** The human at the terminal. Used for:

- Status bar display (the human sees their own name)
- `/read` section headers (messages grouped by addressee)
- Presence in `/who` (human is independently addressable)
- Receiving messages addressed to the human (`/write @jfreeman`)

**Invariant:** The primary is ALWAYS the agent identity. The companion
is ALWAYS the human identity. There is no swapping, no "late
correction," and no state where `config.user` is reassigned after
startup. The primary identity is determined once, at process start,
from files on disk.

## 2. Identity Resolution Sources

| Source | What it provides | Availability | Race risk |
|---|---|---|---|
| `.punt-labs/ethos.yaml` | `agent` field: the handle of the agent identity for this repo | At process start (file on disk) | None |
| `.punt-labs/ethos/identities/{handle}.yaml` | `name`, `handle`, `kind`, `email`, `github` for any identity | At process start (files on disk) | None |
| `ethos session roster --json` | Both identities (root + primary) with roles | After SessionStart hook runs `ethos iam` | Race on `claude --resume` |
| `ethos whoami --json` | Current session persona (whoever called `ethos iam` most recently) | After SessionStart hook | Race on `claude --resume` |
| `gh api user` | GitHub username + display name | Network call, ~200ms | Timeout possible |
| OS username (`getpass.getuser()`) | Login name | Always | None |

### Key observation

The first two sources -- `.punt-labs/ethos.yaml` and the identity YAML
files -- are plain files on disk. They are available the instant the
process starts. No subprocess call, no hook dependency, no race.

The `ethos whoami` and `ethos session roster` commands depend on the
SessionStart hook having already run `ethos iam`. On `claude --resume`,
the MCP server process starts before the SessionStart hook fires. This
is the race that produced 8 releases of patches.

## 3. Resolution Algorithm

### 3.1 Primary (agent)

The agent identity is resolved from files on disk at startup. No
subprocess call. The algorithm:

1. Find the git repository root (existing `find_git_root()`).

2. Read `.punt-labs/ethos.yaml` (or legacy fallback
   `.punt-labs/ethos/config.yaml`). Parse the `agent` field. This field
   contains the handle of the agent identity -- e.g., `"claude"`.

   **Exact file:** `{repo_root}/.punt-labs/ethos.yaml`
   **Exact field:** `agent` (YAML string)
   **Example contents:**

   ```yaml
   agent: claude
   team: engineering
   ```

3. If the `agent` field is present and non-empty, read the
   corresponding identity file:
   `{repo_root}/.punt-labs/ethos/identities/{agent}.yaml`

   Extract `handle`, `name` (mapped to `display_name`), and `kind`.

   **Example:** `.punt-labs/ethos/identities/claude.yaml`

   ```yaml
   name: Claude Agento
   handle: claude
   kind: agent
   email: claude@punt-labs.com
   github: claude-puntlabs
   ```

   This yields: `user="claude"`, `display_name="Claude Agento"`,
   `kind="agent"`.

4. If step 2 fails (file missing, parse error, `agent` field empty),
   or step 3 fails (identity file missing or unparseable):
   fall through to existing chain: `gh api user` > OS username.
   `display_name` and `kind` are best-effort from whichever source
   succeeds.

5. The resolved identity is written into `config.user`,
   `config.display_name`, and `config.kind`. These fields never
   change after this point.

### 3.2 Companion (human)

The companion identity is deferred. It is not available at process
start because the ethos roster requires the SessionStart hook to have
run.

The algorithm:

1. On each heartbeat tick (default: 60s interval), if
   `state.companion` is `None` and companion resolution has not yet
   succeeded:

2. Call `get_ethos_roster()` (subprocess: `ethos session roster --json`,
   2s timeout).

3. If the roster returns two distinct identities (root and primary with
   different handles):

   a. The companion is whichever roster identity is NOT `config.user`
      (the primary).

   b. Create a `CompanionSession` with the companion's handle,
      display_name, and kind.

   c. Register the companion session (claim TTY name, write KV row,
      write active-marker, append wtmp login event).

   d. On success: set `state.companion`. Stop polling.

   e. On failure: rollback (clear `state.companion`). Retry on the
      next heartbeat tick. No limit on retries -- the cost is one
      subprocess call per heartbeat interval.

4. If the roster is unavailable, returns only one identity, or both
   identities have the same handle: do nothing. Try again on the next
   tick.

5. After the first successful companion registration, stop polling.
   The companion identity is fixed for the session lifetime.

### 3.3 Why not read human identity from disk too?

The identity YAML files contain all team members. Without the roster,
there is no way to know WHICH human is at the terminal. The
`jfreeman.yaml` file exists, but so do files for every other identity.
The roster is the only source that says "jfreeman is the root of THIS
session."

A future optimization could cache the roster from the previous session
and use it as a warm start, but that introduces staleness risk (a
different human could resume the session). The heartbeat polling
approach is correct and adds at most 60s of latency to companion
registration.

## 4. State Transitions

```text
STARTUP --> AGENT_ONLY --> DUAL_SESSION
                       \-> AGENT_ONLY (permanent)
```

### States

**STARTUP.** The MCP server is initializing. No sessions are registered
in KV. `config.user` is being resolved from disk. Duration: <100ms
(file reads only, no subprocess calls for the primary identity).

**AGENT_ONLY.** The primary (agent) session is registered in NATS KV.
`config.user` is set. `state.companion` is `None`. The status bar shows
the agent identity. `/read` shows a flat list. Heartbeat polls for the
roster on each tick.

**DUAL_SESSION.** Both sessions are registered. `state.companion` is
set. The status bar shows the human identity. `/read` shows per-identity
sections. Heartbeat maintains both sessions. No further roster polling.

### Transitions

**STARTUP -> AGENT_ONLY.** Triggered by: lifespan startup completing
`register_session()` for the primary identity. Precondition:
`config.user` resolved from disk (or fallback). Postcondition: KV row
exists for `{config.user}:{tty}`, wtmp login event appended.

**AGENT_ONLY -> DUAL_SESSION.** Triggered by: a heartbeat tick where
`get_ethos_roster()` returns a roster with two distinct identities.
Precondition: `state.companion is None`, roster provides a handle
different from `config.user`. Postcondition: companion KV row exists,
companion wtmp login event appended, `state.companion` is set.

**AGENT_ONLY -> AGENT_ONLY (permanent).** Triggered by: ethos is not
installed, not configured, identity files are missing, or the roster
never provides a second identity. The system remains fully functional
in single-session mode indefinitely. No error, no degradation.

### Non-transitions

**DUAL_SESSION -> AGENT_ONLY** does not occur. Once the companion is
registered, it remains for the session lifetime. Companion heartbeat
failure does not remove the companion -- it only means the KV entry may
expire (and be re-registered on the next successful heartbeat).

**STARTUP -> DUAL_SESSION** does not occur. The companion is never
registered during STARTUP because the roster is not available yet (the
SessionStart hook has not run). Even if the roster happens to be
available (e.g., a cached session from a prior run), the architecture
requires the primary to be registered first.

## 5. Invariants

These properties must hold at all times after STARTUP completes:

1. **`config.user` is the agent identity.** It is set once during
   STARTUP from files on disk and never changes. No subprocess call
   contributes to its value.

2. **The primary session key is always the agent.** The KV row at
   `{repo}.{config.user}.{tty}` is the primary session. Outbound
   messages, plans, and heartbeats use this key.

3. **Outbound messages show the agent identity.** The FROM field of
   `/write`, `/wall`, and `/talk` is always `config.user`. The
   companion identity never appears as a message sender.

4. **The companion is always the human.** When `state.companion` is
   not `None`, `state.companion.user` is a human identity (different
   from `config.user`). It is never another agent.

5. **Status bar shows the human when dual-session is active.** The
   unread file's `user` field is `state.companion.user` when the
   companion is registered; `config.user` otherwise.

6. **`/read` shows sections when dual-session is active.** Messages
   are grouped by addressee identity. When `state.companion` is
   `None`, the flat format is used.

7. **The AGENT_ONLY -> DUAL_SESSION transition happens at most once.**
   After the companion is registered, heartbeat stops polling the
   roster. The companion identity is fixed for the session lifetime.

8. **Ethos absence is not an error.** If ethos is not installed, the
   identity files do not exist, or the roster is never available, the
   system operates in AGENT_ONLY mode permanently. No warning, no
   degradation, no retry storm. The fallback chain (`gh api user` > OS
   username) provides the primary identity.

9. **No subprocess call on the critical startup path for identity.**
   The primary identity resolution reads `.punt-labs/ethos.yaml` and
   `.punt-labs/ethos/identities/{agent}.yaml` -- both are `Path.read_text()`
   calls. The `ethos whoami --json` call is eliminated from the primary
   identity path. (Note: `get_ethos_team()` and `get_github_identity()`
   are still subprocess calls on the startup path, but they do not
   contribute to `config.user` when ethos identity files are present.)

## 6. Failure Modes

### 6.1 Ethos not installed

**Condition:** The `ethos` binary is not on PATH. `.punt-labs/ethos.yaml`
may or may not exist.

**Behavior:** If `.punt-labs/ethos.yaml` exists and contains `agent:
claude`, and `.punt-labs/ethos/identities/claude.yaml` exists, the
primary identity is resolved from files. The companion is never
registered (roster calls fail with `FileNotFoundError`). System
operates in AGENT_ONLY permanently.

If the identity files also do not exist, fall through to `gh api user`
or OS username. Same AGENT_ONLY permanent state.

### 6.2 Ethos installed but identity files missing

**Condition:** `.punt-labs/ethos.yaml` does not exist, or its `agent`
field is empty, or the referenced identity YAML does not exist.

**Behavior:** Fall through to existing resolution chain: `gh api user`
> OS username. Companion registration is attempted on heartbeat ticks
via `get_ethos_roster()` but may also fail if ethos is not configured.
System likely stays AGENT_ONLY.

### 6.3 Ethos installed, identity files present, roster never available

**Condition:** The SessionStart hook never runs (e.g., hook
misconfiguration), or `ethos session roster --json` always returns an
error or a single-identity roster.

**Behavior:** Primary identity is resolved from disk. Companion is
never registered. System operates in AGENT_ONLY permanently. Heartbeat
continues to call `get_ethos_roster()` on each tick -- the cost is one
subprocess call every 60s, which is negligible.

**Design choice:** Do not add a retry limit. The heartbeat already
gates on `state.companion is None`, so after success, polling stops.
Before success, the cost is bounded by the heartbeat interval. A retry
limit would create a new failure mode ("gave up too early") without
meaningful benefit.

### 6.4 Companion registration fails (NATS error)

**Condition:** `get_ethos_roster()` succeeds and returns two distinct
identities, but `register_session()` for the companion fails (NATS
connection error, KV write timeout, TTY name claim failure).

**Behavior:** Rollback `state.companion` to `None`. Log a warning.
Retry on the next heartbeat tick. The system remains in AGENT_ONLY
until registration succeeds. There is no limit on retries -- each
attempt is independent and the roster data is re-fetched each time.

### 6.5 Companion heartbeat fails after successful registration

**Condition:** The companion is registered (DUAL_SESSION state), but a
subsequent heartbeat call for the companion session fails.

**Behavior:** Existing heartbeat error handling applies. The heartbeat
loop logs a warning and continues. The companion KV entry may expire if
heartbeats fail for the full TTL duration (default: 3 days for NATS
KV). On the next successful heartbeat, the companion KV entry is
refreshed. The companion is NOT removed from `state.companion` -- it
remains registered locally even if the KV entry temporarily expires.

### 6.6 `.punt-labs/ethos.yaml` is malformed

**Condition:** The file exists but contains invalid YAML or the `agent`
field is not a string.

**Behavior:** Treat as if the file does not exist. Fall through to
`gh api user` > OS username. Log a warning so the user can fix the
config. Do not raise `SystemExit` -- a broken ethos config should not
prevent biff from starting.

### 6.7 Identity YAML has `kind: human` for the agent handle

**Condition:** `.punt-labs/ethos.yaml` says `agent: jfreeman` but
`jfreeman.yaml` has `kind: human`.

**Behavior:** Accept it. The `kind` field is informational for display
(the `[A]` tag in `/who`). The identity resolution algorithm does not
filter by kind -- it trusts the `agent` field in the repo config. If
the repo config says `agent: jfreeman`, then jfreeman is the primary.
This is a misconfiguration but not an error.

## 7. What This Replaces

This spec eliminates the following workarounds and race-prone code
paths:

### 7.1 `get_ethos_identity()` for primary resolution

**Current:** `load_config()` calls `get_ethos_identity()` (subprocess:
`ethos whoami --json`) to resolve `config.user`. This races the
SessionStart hook on `claude --resume`.

**New:** Primary identity resolved from `.punt-labs/ethos.yaml` +
`.punt-labs/ethos/identities/{agent}.yaml`. No subprocess. The
`get_ethos_identity()` function is removed from the primary identity
path. It may be retained for backward compatibility in other callers
(e.g., CLI session) but is no longer on the MCP server startup critical
path.

### 7.2 `_try_late_companion_registration` identity race detection

**Current:** The function in `app.py` detects that `config.user`
(which may have resolved to the human due to the `ethos whoami` race)
differs from the roster's primary, and registers a companion for
"whichever identity is NOT `config.user`."

**New:** `config.user` is always the agent (resolved from disk).
Companion registration still occurs via heartbeat polling, but the
logic is simpler: the companion is always the roster's root (human)
identity. The "which identity is NOT config.user" branch is replaced
by "companion is roster.root if roster.root.handle != config.user."
The conceptual model changes from "detect and correct a race" to
"wait for data that is not yet available."

### 7.3 The `config.user == root` bail-out

**Current:** In `load_config()`, the roster check includes
`roster.root.handle != user` to detect whether the roster's root is
different from the resolved primary. On resume, `user` might be the
human (from `ethos whoami` returning the stale/root identity), making
this check incorrectly evaluate to `False` and skipping companion
registration.

**New:** `config.user` is always the agent. The check
`roster.root.handle != config.user` always correctly identifies the
human as different from the agent. No ambiguity.

### 7.4 "Late companion" as a distinct code path

**Current:** Two paths can create a companion: (a) at startup in
`load_config()` + `_active_lifespan`, and (b) on the first heartbeat
tick via `_try_late_companion_registration`. Both paths have subtly
different error handling and rollback semantics.

**New:** One path. The companion is never registered at startup (the
roster is not available yet). It is always registered via heartbeat
polling. The startup path sets up the primary only. The heartbeat path
handles companion registration exclusively. One code path, one set of
error handling, one set of tests.

### 7.5 The assumption that `ethos whoami` is available at MCP startup

**Current:** `load_config()` calls `get_ethos_identity()` synchronously
during config resolution. On fresh sessions, the SessionStart hook has
already run by the time the MCP server starts. On resumed sessions, the
hook has not run yet. This timing dependency is the root cause of the
identity race.

**New:** The primary identity path has zero dependency on any ethos
subprocess. The files are on disk. The timing of the SessionStart hook
is irrelevant for primary identity resolution. The companion path
explicitly acknowledges the timing dependency and handles it via
polling.

### 7.6 Roster resolution during `load_config()`

**Current:** `load_config()` calls `get_ethos_roster()` to determine
`root_identity` during synchronous config resolution. This adds a
subprocess call (2s timeout) to the startup critical path even though
the roster may not be available yet.

**New:** `load_config()` does not call `get_ethos_roster()`.
`ResolvedConfig.root_identity` is removed. Roster resolution is
entirely deferred to the heartbeat loop, which runs asynchronously
after the server is fully started.
