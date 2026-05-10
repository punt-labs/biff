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

3. **Validate the `agent` handle** before using it to build a path.
   The handle must match `^[a-z0-9][a-z0-9_-]*$`, length 1--64. Reject
   any value containing `/`, `\`, `..`, a leading `.`, or characters
   outside `[a-z0-9_-]`. This is a hard precondition -- the file
   `.punt-labs/ethos.yaml` is repository content under the control of
   anyone who can push a commit, so the handle is untrusted input.

4. If the `agent` field is present, non-empty, and passes validation,
   read the corresponding identity file:
   `{repo_root}/.punt-labs/ethos/identities/{agent}.yaml`

   Before opening the file, call `identity_path.resolve()` and verify
   the resolved path is a descendant of
   `(repo_root / ".punt-labs" / "ethos" / "identities").resolve()`.
   If not, treat as failure. This guards against path-traversal
   handles that slip past the regex.

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

5. **Require `kind: agent` on the resolved identity.** If the identity
   YAML's `kind` field is not the exact string `agent`, the resolution
   fails. This prevents a repo-controlled `.punt-labs/ethos.yaml` from
   coercing biff into registering or sending as a human (or any other
   kind). Failure here falls through to step 6 -- it does not abort.

6. **Advisory ethos team check.** If `ethos team show --json` (or
   equivalent local team data) is available, the resolved handle
   SHOULD appear in the active team's member list. A mismatch is
   logged as a warning and the resolution continues -- absence of
   ethos must not break biff (invariant 8). Team data is consulted
   only when readily available from disk; biff MUST NOT block startup
   on a subprocess call to validate the handle.

7. If step 2 fails (file missing, parse error, `agent` field empty,
   handle fails validation in step 3), or step 4 fails (identity file
   missing, unparseable, or outside the identities directory), or
   step 5 fails (`kind` is not `agent`): fall through to existing
   chain: `gh api user` > OS username. `display_name` and `kind` are
   best-effort from whichever source succeeds.

8. The resolved identity is written into `config.user`,
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

2. Call `get_ethos_roster()` off the event loop via
   `asyncio.to_thread(get_ethos_roster)`. The heartbeat MUST NOT call
   `get_ethos_roster()` synchronously -- the underlying
   `subprocess.run(..., timeout=2)` would block the event loop for up
   to 2s per tick, stalling inbox polling and tool responsiveness.

3. If the roster returns two distinct identities (root and primary with
   different handles):

   a. **The companion is always `roster.root`**[^roster-root]. Ethos
      defines the roster root as the no-parent participant in the
      session graph -- which for biff is always the human at the
      terminal. The earlier "whichever identity is NOT `config.user`"
      rule was a workaround from the period when `config.user` could
      race onto the human identity; with agent-first resolution
      (section 3.1), `config.user` is always the agent, so the
      simpler rule suffices and the workaround is removed.

   b. If `roster.root.handle == config.user`, there is no companion to
      register (the agent is also the root, an unusual configuration).
      Do nothing; do not retry until the roster changes.

   c. Create a `CompanionSession` with `roster.root`'s handle,
      display_name, and kind.

   d. Register the companion session (claim TTY name, write KV row,
      write active-marker, append wtmp login event).

   e. On success: set `state.companion`. Stop polling.

   f. On failure: rollback (clear `state.companion`). Retry on the
      next heartbeat tick. No limit on retries -- the cost is one
      subprocess call per heartbeat interval.

4. If the roster is unavailable, returns only one identity, or both
   identities have the same handle: do nothing. Try again on the next
   tick.

5. After the first successful companion registration, stop polling.
   The companion identity is fixed for the session lifetime.

[^roster-root]: Ethos roster contract: `roster.root` is the
participant with no parent in the session graph. SessionStart wires
the human as root and the agent persona as primary via `ethos iam`.
See `ethos session roster --json` schema.

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

10. **The primary identity must have `kind: agent`.** A repo-controlled
    `.punt-labs/ethos.yaml` cannot escalate a human (or any non-agent
    `kind`) into the agent slot. If the identity YAML's `kind` field
    is not the exact string `agent`, the disk-resolution path is
    rejected and the fallback chain (`gh api user` > OS username) is
    consulted. The `agent` handle is also validated against the
    grammar `^[a-z0-9][a-z0-9_-]*$` (length 1--64), and the resolved
    identity path must stay within
    `{repo_root}/.punt-labs/ethos/identities/` -- this prevents
    handle-based path traversal.

11. **Heartbeat tick latency is bounded.** A single heartbeat tick
    MUST NOT block the asyncio event loop for more than 100ms. All
    subprocess calls inside the heartbeat (notably
    `get_ethos_roster()`) run via `asyncio.to_thread(...)`. The 2s
    subprocess timeout applies to the worker thread, not to the
    event loop.

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

**Behavior:** Reject the disk-resolution path (invariant 10). The
agent slot must be filled by an identity with `kind: agent`; a
repo-controlled `ethos.yaml` cannot escalate a human into the agent
slot, even by accident. Resolution falls through to `gh api user` >
OS username. Log a warning naming the file and the rejected `kind`
value so the operator can fix it.

### 6.8 `.punt-labs/ethos.yaml` agent handle fails validation

**Condition:** The `agent` field contains characters outside
`[a-z0-9_-]`, starts with `.`, contains `..` or `/`, or is longer
than 64 characters.

**Behavior:** Reject (invariant 10). Resolution falls through to
`gh api user` > OS username. Log a warning naming the offending
value. This is the path-traversal guard: handles like
`../../etc/passwd` never reach the filesystem.

## 7. What This Replaces

This spec eliminates the following workarounds and race-prone code
paths:

### 7.1 `get_ethos_identity()` for primary resolution

**Current:** `load_config()` calls `get_ethos_identity()` (subprocess:
`ethos whoami --json`) to resolve `config.user`. This races the
SessionStart hook on `claude --resume`.

**New:** Primary identity resolved from `.punt-labs/ethos.yaml` +
`.punt-labs/ethos/identities/{agent}.yaml`. No subprocess. The
`get_ethos_identity()` function and its tests are deleted -- after
the new resolver is wired into `load_config()`, no caller remains in
`src/`. `cli_session.py` reaches the identity through `load_config()`
and so inherits the new behavior without any of its own changes.

### 7.2 `_try_late_companion_registration` identity race detection

**Current:** The function in `app.py` detects that `config.user`
(which may have resolved to the human due to the `ethos whoami` race)
differs from the roster's primary, and registers a companion for
"whichever identity is NOT `config.user`."

**New:** `config.user` is always the agent (resolved from disk).
Companion registration still occurs via heartbeat polling, but the
logic is simpler: the companion is always `roster.root` (the human;
see section 3.2 and the ethos roster contract). The
"whichever identity is NOT `config.user`" branch is deleted -- it
existed only to repair the race between `ethos whoami` and
SessionStart. When `roster.root.handle == config.user`, there is no
companion to register and the heartbeat returns without state change.
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
