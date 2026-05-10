# Change Plan: Agent-First Identity Resolution

**Spec:** `docs/spec-agent-first-identity.md`
**Bead:** biff-8fg3
**Pipeline:** formal-2026-04-16-dc439e, stage 2 (design)

## 1. Files Changed

### `src/biff/config.py`

| Function | Action | What changes |
|---|---|---|
| `resolve_agent_identity_from_disk` | **ADD** | New function. Reads `.punt-labs/ethos.yaml` and the referenced identity YAML. Returns `EthosIdentity` or `None`. |
| `load_config` | **MODIFY** | Replace `get_ethos_identity()` with `resolve_agent_identity_from_disk(repo_root)` in the identity chain. Remove `get_ethos_roster()` call that populates `root_identity`. |
| `ResolvedConfig` | **MODIFY** | Remove the `root_identity` field. |

`get_ethos_identity()`, `get_ethos_roster()`, and all roster-parsing helpers
(`_parse_roster_entry`, `_parse_roster_participants`, `_parse_roster_legacy`,
`EthosRoster`) are **retained** -- they are still called from `app.py`'s
heartbeat path for companion resolution and from `cli_session.py`.

### `src/biff/__main__.py`

| Function | Action | What changes |
|---|---|---|
| `_create_mcp_server` | **MODIFY** | Remove the `resolved.root_identity` block that creates `CompanionSession` at startup. Pass `companion=None` always. |

### `src/biff/server/app.py`

| Function | Action | What changes |
|---|---|---|
| `_try_late_companion_registration` | **RENAME** to `_poll_companion_registration` | Remove "late" framing. Remove the identity-race comment. Simplify: companion is always `roster.root` when `roster.root.handle != config.user`. |
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
| **ADD** tests for `resolve_agent_identity_from_disk` | Happy path, missing ethos.yaml, missing identity YAML, malformed YAML, empty agent field, agent with `kind: human`. |
| **MODIFY** existing `load_config` tests | Remove assertions on `root_identity`. Update identity resolution mocks. |

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
    parse errors, empty fields) -- never raises.
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
6. Compute `identity_path = repo_root / ".punt-labs" / "ethos" / "identities" / f"{agent}.yaml"`.
7. If the file does not exist, return `None`.
8. Call `_load_yaml(identity_path)`. On empty dict, return `None`.
9. Extract `handle` (fall back to `agent`), `name` (fall back to `handle`),
   `kind` (fall back to `""`).
10. Return `EthosIdentity(handle=handle, display_name=name, kind=kind)`.

**Error handling:** Every failure path returns `None`. The function logs
warnings for malformed YAML (helps operators diagnose config issues) but
never raises. `_load_yaml` already catches `OSError` and `UnicodeDecodeError`,
returning `{}`. `yaml.YAMLError` needs a try/except wrapper here (unlike
`load_yaml_config` which raises `SystemExit` on parse errors, identity
config errors must not prevent biff from starting -- spec invariant 8).

**Call site:** `load_config()`, line ~720 (current), replacing
`get_ethos_identity()`. Called BEFORE any subprocess call contributes
to `config.user`.

## 3. Changes to `load_config`

**Current chain** (lines 717-731):

```text
CLI override -> get_ethos_identity() -> get_github_identity() -> get_os_user()
```

Where `get_ethos_identity()` is `subprocess.run(["ethos", "whoami", "--json"])`.

**New chain:**

```text
CLI override -> resolve_agent_identity_from_disk(repo_root) -> get_github_identity() -> get_os_user()
```

Where `resolve_agent_identity_from_disk(repo_root)` reads two YAML files
from disk. No subprocess call.

**Specific code changes in `load_config`:**

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
if roster.root.handle == state.config.user:
    return  # Root IS the agent -- no human companion to register
companion_identity = roster.root
```

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
| `get_ethos_identity()` usage for primary identity | `load_config()` line ~720 | Replaced by `resolve_agent_identity_from_disk()` |
| `get_ethos_roster()` usage for `root_identity` | `load_config()` lines 748-757 | Roster resolution deferred to heartbeat |
| `ResolvedConfig.root_identity` field | `config.py` line 63 | No longer populated at config time |
| `CompanionSession` creation in `_create_mcp_server` | `__main__.py` lines 1026-1033 | Companion creation deferred to heartbeat |
| `await _register_companion(state)` at startup | `app.py` line 941 | Companion registration deferred to heartbeat |
| Companion login event at startup | `app.py` lines 955-956 | Follows from above |
| `_companion_checked` one-shot flag | `app.py` `_heartbeat_loop` line 326 | Companion polls every tick until success |

### Functions / code NOT deleted (retained)

| What | Why retained |
|---|---|
| `get_ethos_identity()` function definition | May be used by CLI or other callers |
| `get_ethos_roster()` function definition | Called by `_poll_companion_registration` on heartbeat |
| Roster parsing helpers | Called by `get_ethos_roster()` |
| `_register_companion()` | Called from `_poll_companion_registration` (heartbeat path) |
| `_append_companion_login_event()` | Called from `_poll_companion_registration` |
| `_append_companion_logout_event()` | Called from `_lifespan_cleanup` |
| Signal handler companion blocks | Companion may exist at signal time (registered during heartbeat) |

### Race detection code removed

The `roster.root.handle != state.config.user` bail-out in `load_config()` is
deleted (the entire roster block is removed from `load_config`).

The `roster.primary.handle != state.config.user` race detection in
`_try_late_companion_registration` is replaced with simpler logic: companion
is always `roster.root` when `roster.root.handle != config.user`.

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

The CLI session calls `load_config()` which will now use
`resolve_agent_identity_from_disk()` instead of `get_ethos_identity()`.
CLI commands (`biff who`, `biff write`, etc.) will identify as the agent.
This is consistent -- the CLI is invoked from the agent's process.

No changes to `cli_session.py` are needed. The new identity resolution
happens inside `load_config()` which `cli_session` already calls.

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

### Step 2: Wire `resolve_agent_identity_from_disk` into `load_config`

Replace `get_ethos_identity()` with `resolve_agent_identity_from_disk(repo_root)`
in the identity resolution chain. Remove the roster block that populates
`root_identity`. Remove the `root_identity` field from `ResolvedConfig`.

Update existing `load_config` tests that mock `get_ethos_identity` to
instead mock `resolve_agent_identity_from_disk` or provide fixture files.
Update any test that references `resolved.root_identity`.

**Verification:** `make check` passes. Identity resolution uses disk reads.

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
Simplify the companion selection logic (always `roster.root`). Remove the
`_companion_checked` one-shot guard from `_heartbeat_loop` -- poll every
tick while `state.companion is None`.

Update docstrings to reflect the new model.

**Verification:** `make check` passes. Full integration test: companion
is registered on the first heartbeat tick after the roster becomes
available.

### Step 6: Update documentation

Add a new ADR entry to `DESIGN.md` documenting the agent-first identity
model. Reference this change plan and the spec. Update any existing ADRs
that reference the old identity resolution path (DES-039 companion
registration, identity race detection).

**Verification:** `make check` passes (markdownlint).
