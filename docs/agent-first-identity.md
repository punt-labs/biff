# Change Plan: Agent-First Identity Resolution

**Spec:** `docs/spec-agent-first-identity.md`
**Bead:** biff-8fg3
**Pipeline:** formal-2026-04-16-dc439e, stage 2 (design)

## 1. Files Changed

### `src/biff/config.py`

| Function | Action | What changes |
|---|---|---|
| `resolve_agent_identity_from_disk` | **ADD** | New function. Reads `.punt-labs/ethos.yaml` and the referenced identity YAML. Returns `EthosIdentity` or `None`. |
| `load_mcp_config` | **ADD** | Renamed entry point for MCP server config (formerly `load_config`). Calls `resolve_agent_identity_from_disk(repo_root)` in the identity chain. Identifies as the agent. |
| `load_cli_config` | **ADD** | Entry point for `biff` CLI invocations. Skips disk-based agent resolution; uses the human-identity chain (`--user` override -> `get_github_identity()` -> `get_os_user()`). Identifies as the human at the terminal. |
| `load_config` | **REMOVE** | Replaced by the two functions above. No "default context" -- callers pick the one that matches their context. This avoids the trap where a CLI caller silently inherits MCP-server behavior. |
| `ResolvedConfig` | **MODIFY** | Remove the `root_identity` field. |

`get_ethos_roster()` and all roster-parsing helpers (`_parse_roster_entry`,
`_parse_roster_participants`, `_parse_roster_legacy`, `EthosRoster`) are
**retained** -- they are still called from `app.py`'s heartbeat path for
companion resolution.

`get_ethos_identity()` is **deleted** along with its tests. Before this
change it was called only by `load_config()`. After the split, neither
`load_mcp_config` nor `load_cli_config` calls it: the MCP entry uses
the new disk resolver, and the CLI entry uses the human-identity chain
directly. No caller remains in `src/`. Keeping a dead helper for
"ad-hoc CLI debugging" violates the project's "no backwards-compatibility
shims" rule (CLAUDE.md). Section 6 lists this deletion explicitly.

**Why two functions, not one with a `context` flag.** A flag-based API
(`load_config(context="mcp" | "cli")`) places the burden of picking
the right context on every caller, and the default value -- whichever
we pick -- silently mis-identifies callers in the other context. Two
named functions make the choice explicit at the call site and make
grep/IDE navigation surface every caller of each path. The shared
config-loading machinery (relay URL, repo name, team enrichment,
peers) lives in a private helper that both call.

### `src/biff/__main__.py`

| Function | Action | What changes |
|---|---|---|
| `_create_mcp_server` | **MODIFY** | Remove the `resolved.root_identity` block that creates `CompanionSession` at startup. Pass `companion=None` always. |

### `src/biff/server/app.py`

| Function | Action | What changes |
|---|---|---|
| `_try_late_companion_registration` | **RENAME** to `_poll_companion_registration` | Remove "late" framing. Remove the identity-race comment. Simplify: companion is always `roster.root`. Invoke `get_ethos_roster()` via `asyncio.to_thread` so the subprocess never blocks the event loop (spec invariant 11). |
| `_heartbeat_loop` | **MODIFY** | Remove the `_companion_checked` one-shot guard. Poll for companion on every tick while `state.companion is None`. Stop polling after first success (existing `state.companion is None` guard suffices). |
| `_active_lifespan` | **MODIFY** | Remove the startup `await _register_companion(state)` call and the companion login event at startup. The companion is never registered during lifespan startup. |

### `src/biff/server/state.py`

No structural changes. `CompanionSession` and `ServerState` remain as-is.
The `companion` field on `ServerState` starts as `None` and is set by the
heartbeat path -- same field, different timing.

### `src/biff/server/tools/_descriptions.py`

| Function | Action | What changes |
|---|---|---|
| `_sync_unread_file` | **NO CHANGE** | Already handles `state.companion is None` correctly (falls back to `config.user`). No modification needed. |

### `src/biff/server/tools/messaging.py`

| Function | Action | What changes |
|---|---|---|
| `read_messages` | **NO CHANGE** | Already handles `state.companion is None` correctly (returns flat format). No modification needed. |

### `tests/test_config.py`

| Change | What |
|---|---|
| **ADD** tests for `resolve_agent_identity_from_disk` | Happy path; missing ethos.yaml; missing identity YAML; malformed YAML; empty agent field; **`kind: human` (must return `None`, fall through to fallback chain)**; **path-traversal handles `../../etc/passwd`, `../foo`, `foo/bar` (all rejected by the regex)**; **handle that passes the regex but resolves outside `identities/` (rejected by the `is_relative_to` check)**. |
| **MODIFY** existing `load_config` tests | Remove assertions on `root_identity`. Replace `get_ethos_identity` mocks with `resolve_agent_identity_from_disk` mocks or on-disk fixture files. |
| **DELETE** `get_ethos_identity` tests | After step 2, no caller remains. Delete the helper and its tests together. |

### `tests/test_server/test_lifespan_registration.py`

| Change | What |
|---|---|
| **MODIFY** companion registration tests | Companion is no longer created at startup. Tests that set `companion` on the state fixture must be updated to verify it happens during heartbeat instead. |

## 2. New Function: `resolve_agent_identity_from_disk`

```python
def resolve_agent_identity_from_disk(repo_root: Path) -> EthosIdentity | None:
    """Resolve agent identity from ethos config files on disk.

    Reads ``{repo_root}/.punt-labs/ethos.yaml`` for the ``agent`` field,
    then ``{repo_root}/.punt-labs/ethos/identities/{agent}.yaml`` for
    identity details. Returns ``None`` on any failure (missing files,
    parse errors, empty fields, invalid handle grammar, path-traversal
    attempt, or ``kind != "agent"``) -- never raises.
    """
```

**Input:** `repo_root: Path` -- the git repository root, already resolved
by `find_git_root()`.

**Output:** `EthosIdentity | None` -- populated with `handle`, `display_name`,
and `kind` from the identity YAML; `None` on any failure.

**Algorithm:**

1. Compute `ethos_config = repo_root / ".punt-labs" / "ethos.yaml"`.
2. If the file does not exist, try legacy fallback
   `repo_root / ".punt-labs" / "ethos" / "config.yaml"`.
3. If neither exists, return `None`.
4. Call `_load_yaml(path)` (already exists in `config.py`). On empty dict or
   `yaml.YAMLError`, log a warning and return `None`.
5. Extract the `agent` field. If missing, not a string, or empty after
   stripping, return `None`.
6. **Validate the agent handle.** Match against
   `re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")`. Reject any string
   containing `/`, `\`, `..`, a leading `.`, or characters outside
   `[a-z0-9_-]`. On rejection, log a warning naming the offending
   value and return `None`. `.punt-labs/ethos.yaml` is
   repository-controlled, so the handle is untrusted -- without this
   gate, `agent: ../../etc/passwd` (or any other traversal payload)
   would build a path outside the identities directory.
7. Compute `identities_root = (repo_root / ".punt-labs" / "ethos" / "identities").resolve()`
   and `identity_path = (identities_root / f"{agent}.yaml").resolve()`.
   Verify `identity_path.is_relative_to(identities_root)` (use
   `pathlib.PurePath.is_relative_to` directly; Python 3.13+).
   If the check fails, log a warning and return `None`. This is a
   defense-in-depth guard for any traversal payload that slipped past
   the regex (e.g., via locale-specific normalization).
8. If the file does not exist, return `None`.
9. Call `_load_yaml(identity_path)`. On empty dict, return `None`.
10. Extract `handle` (fall back to `agent`), `name` (fall back to `handle`),
    `kind` (fall back to `""`).
11. **Enforce `kind == "agent"`.** If `kind` is any other value, log a
    warning and return `None`. A repo-controlled `.punt-labs/ethos.yaml`
    must not be able to elevate a human (or any non-agent kind) into
    `config.user`. Spec invariant 10.
12. Return `EthosIdentity(handle=handle, display_name=name, kind=kind)`.

**Error handling:** Every failure path returns `None`. The function logs
warnings for malformed YAML (helps operators diagnose config issues) but
never raises. `_load_yaml` already catches `OSError` and `UnicodeDecodeError`,
returning `{}`. `yaml.YAMLError` needs a try/except wrapper here (unlike
`load_yaml_config` which raises `SystemExit` on parse errors, identity
config errors must not prevent biff from starting -- spec invariant 8).

**Call site:** `load_config()`, line ~720 (current), replacing
`get_ethos_identity()`. Called BEFORE any subprocess call contributes
to `config.user`.

## 3. Changes to `load_mcp_config` (formerly `load_config`)

The agent-first algorithm in this section applies to `load_mcp_config`
**only**. `load_cli_config` (section 3a) skips disk-based agent
resolution entirely. Spec § 1.1.

**Current chain** (`load_config`, lines 717-731):

```text
CLI override -> get_ethos_identity() -> get_github_identity() -> get_os_user()
```

Where `get_ethos_identity()` is `subprocess.run(["ethos", "whoami", "--json"])`.

**New chain (`load_mcp_config`):**

```text
user_override -> resolve_agent_identity_from_disk(repo_root) -> get_github_identity() -> get_os_user()
```

Where `resolve_agent_identity_from_disk(repo_root)` reads two YAML files
from disk. No subprocess call.

**Specific code changes in `load_mcp_config`:**

1. Replace the `ethos = get_ethos_identity()` block (lines 720-728) with:

   ```python
   ethos = resolve_agent_identity_from_disk(repo_root)
   ```

   The rest of the block (`if ethos is not None: user = ethos.handle ...`)
   remains identical.

2. Delete the roster resolution block (lines 748-757):

   ```python
   root_identity: EthosIdentity | None = None
   if user_override is None:
       roster = get_ethos_roster()
       ...
   ```

   This block populated `root_identity` for startup companion creation.
   With the new model, companion creation is entirely deferred to heartbeat.

3. Remove `root_identity=root_identity` from the `ResolvedConfig(...)` constructor.

4. Remove the `root_identity` field from `ResolvedConfig`.

**What stays:** `get_ethos_team()` remains on the startup path (called via
`_enrich_team`). It is a subprocess call, but it does not contribute to
`config.user` -- it only populates `config.team`. The spec explicitly
permits this (invariant 9: "get_ethos_team() and get_github_identity() are
still subprocess calls on the startup path, but they do not contribute to
config.user when ethos identity files are present").

## 3a. `load_cli_config`: human identity for the CLI

The `biff` CLI is invoked by a human at the terminal. CLI sessions
identify as the human, not the agent (spec § 1.1).

**Chain:**

```text
user_override -> get_github_identity() -> get_os_user()
```

No call to `resolve_agent_identity_from_disk`. No call to
`get_ethos_identity` (deleted). The CLI never reads
`.punt-labs/ethos.yaml` to pick an identity, because the human at the
terminal is the identity -- their GitHub handle or login name.

**Specific behavior:**

1. If `user_override` is set (e.g., `biff --user alice who`), use it.
2. Otherwise, call `get_github_identity()`. On success, use the GitHub
   login as `user` and the GitHub display name as `display_name`.
3. Otherwise, call `get_os_user()`. Use the OS login as `user` and as
   `display_name`. `kind` defaults to `""` (unknown).
4. Construct a `BiffConfig` with `user`, `display_name`, `kind`, and
   the shared fields (relay URL, repo name, team, peers) loaded via
   the private helper that backs both entry points.

**Rationale:** The agent's identity is a property of the MCP server's
process context. The CLI runs in the user's shell -- typically a
human's interactive terminal -- and inherits the user's identity from
the environment. Conflating these would route a human's `biff write`
message with `FROM=agent`, which is the regression R5 flagged.

**Shared loading.** Both `load_mcp_config` and `load_cli_config` call
a private `_load_base_config(user_override)` that handles the
non-identity portions: repo discovery, relay URL resolution, peer
configuration, and team enrichment. The split is at identity only.

## 4. Changes to `_create_mcp_server` and `_active_lifespan`

### `_create_mcp_server` (`__main__.py`, lines 1004-1043)

Delete the entire companion creation block (lines 1026-1033):

```python
# Dual-session: create companion for the human when ethos roster
# shows two distinct identities (biff-plqr, DES-039).
companion: CompanionSession | None = None
if resolved.root_identity is not None:
    companion = CompanionSession(
        user=resolved.root_identity.handle,
        display_name=resolved.root_identity.display_name,
        kind=resolved.root_identity.kind,
        tty=generate_tty(),
    )
```

The `create_state()` call passes `companion=None` (the default).

### `_active_lifespan` (`app.py`, lines 848-978)

Remove the startup companion registration call (line 941):

```python
await _register_companion(state)
```

Remove the startup companion login event (lines 955-956):

```python
if state.companion:
    await _append_companion_login_event(state)
```

The companion signal handler block in `_signal_handler` (lines 871-883)
remains -- it handles the case where companion was registered during
heartbeat and the process receives a signal.

`_register_companion` itself is **retained** -- it is called from the
heartbeat path (`_poll_companion_registration`). Its logic is unchanged.

## 5. Changes to `_heartbeat_loop` and `_try_late_companion_registration`

### Rename: `_try_late_companion_registration` to `_poll_companion_registration`

The function body simplifies. The current docstring mentions "identity race
on `claude --resume`" -- that framing is obsolete. The new docstring describes
the designed behavior: "Attempt companion registration from ethos roster.
Called on each heartbeat tick while state.companion is None."

**Logic change in the function:**

Current (line 267):

```python
other = roster.root if roster.root.handle != state.config.user else roster.primary
```

This picks "whichever identity is NOT config.user" because config.user could
be either the agent or the human (race). New:

```python
roster = await asyncio.to_thread(get_ethos_roster)
if roster is None:
    return
if roster.root.handle == state.config.user:
    return  # Root IS the agent -- no human companion to register
companion_identity = roster.root
```

`get_ethos_roster()` is invoked through `asyncio.to_thread` so the
underlying `subprocess.run(..., timeout=2)` runs on a worker thread
instead of stalling the event loop for up to 2 seconds per tick. This
keeps inbox polling and tool dispatch responsive even when ethos is
slow or absent. Spec invariant 11 ("Heartbeat tick latency is
bounded") makes this binding.

The companion is always the roster root (the human). If `roster.root.handle
== state.config.user`, it means the agent is also the root (unusual), so
there is no companion. The `roster.primary` branch is removed.

### `_heartbeat_loop` changes

Current guard (lines 338-344):

```python
if state.companion is None and not _companion_checked:
    _companion_checked = True
    try:
        await _try_late_companion_registration(state)
    ...
```

The `_companion_checked` flag makes companion registration attempt at most
once. This was appropriate for the "late" model (if it fails once, the race
is not the issue). In the new model, companion registration should retry on
every tick until it succeeds -- the roster may not be available on the first
tick but will become available after the SessionStart hook runs.

New guard:

```python
if state.companion is None:
    try:
        await _poll_companion_registration(state)
    except Exception:  # noqa: BLE001
        logger.warning("Companion registration poll failed", exc_info=True)
```

No `_companion_checked` flag. No retry limit. The cost is one subprocess call
per heartbeat interval (60s) -- negligible per spec section 6.3. After
`state.companion` is set, the guard short-circuits.

## 6. What Gets Deleted

### Functions / code removed from primary path

| What | Where | Why |
|---|---|---|
| `get_ethos_identity()` function and tests | `config.py`, `tests/test_config.py` | After step 2 of change order, no caller remains in `src/`. `cli_session.py` does not import it. No "ad-hoc CLI debugging" carve-out -- dead helpers violate the no-shims rule. |
| `get_ethos_roster()` usage for `root_identity` | `load_config()` lines 748-757 | Roster resolution deferred to heartbeat |
| `ResolvedConfig.root_identity` field | `config.py` line 63 | No longer populated at config time |
| `CompanionSession` creation in `_create_mcp_server` | `__main__.py` lines 1026-1033 | Companion creation deferred to heartbeat |
| `await _register_companion(state)` at startup | `app.py` line 941 | Companion registration deferred to heartbeat |
| Companion login event at startup | `app.py` lines 955-956 | Follows from above |
| `_companion_checked` one-shot flag | `app.py` `_heartbeat_loop` line 326 | Companion polls every tick until success |
| "whichever identity is NOT config.user" companion selection | `app.py` `_try_late_companion_registration` line 267 | Workaround from the racy past. With agent-first resolution, `config.user` is always the agent, so companion is unambiguously `roster.root`. |

### Functions / code NOT deleted (retained)

| What | Why retained |
|---|---|
| `get_ethos_roster()` function definition | Called by `_poll_companion_registration` on heartbeat |
| Roster parsing helpers | Called by `get_ethos_roster()` |
| `_register_companion()` | Called from `_poll_companion_registration` (heartbeat path) |
| `_append_companion_login_event()` | Called from `_poll_companion_registration` |
| `_append_companion_logout_event()` | Called from `_lifespan_cleanup` |
| Signal handler companion blocks | Companion may exist at signal time (registered during heartbeat) |

### Race detection code removed

The `roster.root.handle != state.config.user` bail-out in `load_config()` is
deleted (the entire roster block is removed from `load_config`).

The "whichever identity is NOT `config.user`" branch in
`_try_late_companion_registration` is deleted. The replacement is
unconditional: `companion = roster.root`. When `roster.root.handle ==
config.user`, no companion registration happens (an unusual configuration
where the agent is also the human at the terminal).

## 7. Migration / Backwards Compatibility

### KV entries from v1.10.0

A v1.10.0 session may have registered with `user=jfreeman` as primary (the
race case). When v1.11.0 starts, it registers `user=claude` as primary.
The old `jfreeman:{tty}` KV entry expires via TTL (3 days default).

**No explicit cleanup needed.** The old entry is inert -- no process is
heartbeating it, so it expires naturally. The orphan detector in
`_close_orphaned_logins` will write a retroactive logout event for it
on the next startup.

### Status bar

The status bar reads `unread.json` which contains `user` and `tty_name`.
During AGENT_ONLY state, `user` will be the agent identity (`claude`).
After DUAL_SESSION transition, `user` switches to the human identity.
This is the correct behavior per spec invariant 5.

Users may notice a brief period (up to 60s) where the status bar shows
`claude` instead of `jfreeman` after startup. This is the designed tradeoff
-- correctness over cosmetics. The spec explicitly permits this in section 4
(AGENT_ONLY state: "The status bar shows the agent identity").

### CLI session (`cli_session.py`)

`cli_session.py` is invoked by a human at the terminal. CLI commands
(`biff who`, `biff write`, etc.) MUST identify as the human, not the
agent. Routing a human's `biff write` with `FROM=agent` would be a
behavioral regression.

**Change:** `cli_session.py` currently calls `load_config(user_override=...)`.
That call site is updated to `load_cli_config(user_override=...)`
(section 3a). The CLI then uses the human-identity chain
(`--user` override -> `get_github_identity()` -> `get_os_user()`),
unchanged from the previous behavior of the implicit chain.

**Why not just keep the old name as the CLI default.** The MCP server
must call into the new agent-first path. If we leave `load_config`
pointing at agent-first resolution and rely on the CLI to opt out via
a flag, the default value will silently mis-identify whichever caller
forgets the flag. The two-function API in section 3 makes the choice
explicit at every call site.

`get_ethos_identity` has no callers after the split: `load_mcp_config`
uses the disk resolver, `load_cli_config` uses the human chain. The
function and its tests are deleted (see section 6).

### Wire protocol

No wire protocol changes. The KV entry format, wtmp event format, and
message format are unchanged. The only difference is which identity
occupies the primary slot.

## 8. Change Order

Each step results in a passing `make check`.

### Step 1: Add `resolve_agent_identity_from_disk`

Add the new function to `src/biff/config.py`. Add tests in
`tests/test_config.py`. The function is not yet called -- existing code
continues to use `get_ethos_identity()`.

**Verification:** `make check` passes. New tests pass. No behavioral change.

### Step 2: Split `load_config` into `load_mcp_config` and `load_cli_config`

Extract the shared (non-identity) portion of `load_config` into a
private `_load_base_config(user_override)` helper. Then introduce two
public entry points:

- `load_mcp_config(user_override=None)` -- calls `_load_base_config`
  and uses the new identity chain
  `user_override -> resolve_agent_identity_from_disk(repo_root) ->
  get_github_identity() -> get_os_user()`.
- `load_cli_config(user_override=None)` -- calls `_load_base_config`
  and uses the human-identity chain
  `user_override -> get_github_identity() -> get_os_user()`
  (the pre-spec behavior, minus the `get_ethos_identity` step).

Remove the roster block that populated `root_identity`. Remove the
`root_identity` field from `ResolvedConfig`. Delete `load_config`.

Update every call site:

- `src/biff/__main__.py` (MCP server entry) -> `load_mcp_config(...)`.
- `src/biff/cli_session.py` -> `load_cli_config(user_override=...)`.

Update existing `load_config` tests:

- Tests that exercised MCP startup -> rename and target `load_mcp_config`.
  Mock `resolve_agent_identity_from_disk` or provide fixture files for
  `.punt-labs/ethos.yaml` + identity YAML.
- Tests that exercised CLI invocation -> rename and target
  `load_cli_config`. Mock `get_github_identity`/`get_os_user`.
- Add a regression test: `load_cli_config` with a fixture
  `.punt-labs/ethos.yaml` + agent identity YAML on disk still returns
  the human identity (proves the CLI path is not silently going
  through the disk resolver).
- Remove assertions on `root_identity`.

**Verification:** `make check` passes. MCP identity resolution uses
disk reads; CLI identity resolution uses the human chain.

### Step 3: Remove startup companion creation from `_create_mcp_server`

Delete the `resolved.root_identity` companion block. `companion=None` always.

Update tests in `test_lifespan_registration.py` that pass a companion
via `resolved.root_identity`.

**Verification:** `make check` passes. No companion at startup. Existing
heartbeat path still creates companion on first tick (via the current
`_try_late_companion_registration`).

### Step 4: Remove startup companion registration from `_active_lifespan`

Remove `await _register_companion(state)` and the companion login event
from `_active_lifespan`. The startup path now registers only the primary.

**Verification:** `make check` passes. Companion registration happens only
via heartbeat.

### Step 5: Refactor heartbeat companion logic

Rename `_try_late_companion_registration` to `_poll_companion_registration`.
Simplify the companion selection logic (always `roster.root`). Wrap the
`get_ethos_roster()` call in `asyncio.to_thread(...)` so the 2s subprocess
timeout runs on a worker thread instead of stalling the event loop -- this
is required by spec invariant 11 ("Heartbeat tick latency is bounded").
Remove the `_companion_checked` one-shot guard from `_heartbeat_loop` --
poll every tick while `state.companion is None`.

Update docstrings to reflect the new model.

**Verification:** `make check` passes. Full integration test: companion
is registered on the first heartbeat tick after the roster becomes
available. Latency test: a stubbed slow `get_ethos_roster` (sleeping
500ms) does not delay other heartbeat work -- inbox polling and KV
heartbeats fire concurrently.

### Step 6: Update documentation

Add a new ADR entry to `DESIGN.md` documenting the agent-first identity
model. Reference this change plan and the spec. Update any existing ADRs
that reference the old identity resolution path (DES-039 companion
registration, identity race detection).

**Verification:** `make check` passes (markdownlint).
