# Ethos Integration Design Spec

**Bead:** biff-n9jg
**Date:** 2026-04-14
**Status:** PROPOSED

## Summary

Biff calls the `ethos` CLI via subprocess to resolve richer identity
and team data. Ethos is optional. Every code path that calls ethos
has a silent fallback to the existing resolution chain. A user who
has never heard of ethos sees no difference.

## Constraint: No Library Dependency

Ethos is a Go binary. Biff is Python. The integration boundary is
`subprocess.run` + JSON on stdout. No `import ethos`. No Go FFI. No
shared library. No protobuf. No gRPC.

## 1. Identity Resolution

### Current chain (`config.py`, lines 499-509)

```text
1. --user CLI override
2. gh api user  ->  GitHubIdentity(login, display_name)
3. getpass.getuser()  ->  OS username
```

### New chain

```text
1. --user CLI override  (unchanged)
2. ethos whoami --json  ->  EthosIdentity(handle, name, kind, github)
3. gh api user  ->  GitHubIdentity(login, display_name)
4. getpass.getuser()  ->  OS username
```

Step 2 is the new step. Steps 3-4 are the existing fallback,
renumbered.

### Subprocess call

```python
subprocess.run(
    ["ethos", "whoami", "--json"],
    capture_output=True,
    text=True,
    check=False,
    timeout=2,
)
```

Timeout: 2 seconds. Ethos is a local Go binary with ~10ms cold
start. A 2-second timeout catches hung processes without penalizing
normal operation.

### JSON fields extracted

`ethos whoami --json` returns the full `Identity` struct. Biff
extracts four fields:

```json
{
  "handle": "claude",
  "name": "Claude Agento",
  "kind": "agent",
  "github": "claude-puntlabs"
}
```

| ethos field | biff field | notes |
|-------------|------------|-------|
| `handle` | `BiffConfig.user` | Primary key for presence, messaging |
| `name` | `BiffConfig.display_name` | Shown in `/finger` header |
| `kind` | `BiffConfig.kind` | New field: `"human"`, `"agent"`, or `""` |
| `github` | (unused) | Reserved for future GitHub-aware features |

### New data model

```python
@dataclass(frozen=True)
class EthosIdentity:
    """Identity resolved from ``ethos whoami --json``."""

    handle: str
    display_name: str
    kind: str  # "human", "agent", or ""
```

Add to `config.py` alongside `GitHubIdentity`.

### New function

```python
def get_ethos_identity() -> EthosIdentity | None:
    """Resolve identity from ethos CLI.

    Returns ``None`` when ethos is not installed, not configured,
    returns malformed JSON, or times out.
    """
```

### Fallback behavior

| Condition | Detection | Result |
|-----------|-----------|--------|
| `ethos` not on PATH | `FileNotFoundError` from `subprocess.run` | Return `None` |
| `ethos whoami` exit 1 | `returncode != 0` | Return `None` |
| `ethos whoami` times out | `subprocess.TimeoutExpired` | Return `None` |
| JSON missing `handle` | KeyError / empty string check | Return `None` |
| JSON malformed | `json.JSONDecodeError` | Return `None` |
| `handle` present, `name` absent | Use `handle` as `display_name` | Return identity |

Every fallback is silent. No warnings, no stderr. The user sees
the same behavior as before ethos existed.

### Integration point

In `load_config()`, replace the identity resolution block
(lines 499-515):

```python
# Resolve user: CLI override > ethos > GitHub > OS username
display_name = ""
kind = ""
if user_override is not None:
    user = user_override
else:
    ethos = get_ethos_identity()
    if ethos is not None:
        user = ethos.handle
        display_name = ethos.display_name
        kind = ethos.kind
    else:
        identity = get_github_identity()
        if identity is not None:
            user = identity.login
            display_name = identity.display_name
        else:
            user = get_os_user()
```

## 2. Team Resolution

### Current behavior (DES-037 zero-config)

Team membership derives from git remote owner:

```text
git remote get-url origin  ->  owner/repo  ->  orgs = (owner,)
```

This discovers repos via NATS `stream_info` (DES-034). There is no
explicit member roster.

### New chain

```text
1. ethos team for-repo --json  ->  team members as handles
2. Existing zero-config  ->  org-based discovery
```

### Subprocess call

```python
subprocess.run(
    ["ethos", "team", "for-repo", "--json"],
    capture_output=True,
    text=True,
    check=False,
    timeout=2,
)
```

No argument needed for current repo. Ethos resolves the repo from
`git remote get-url origin`, same as biff.

### JSON output

`ethos team for-repo --json` returns an array of Team objects.
Typical case: one team.

```json
[
  {
    "name": "engineering",
    "repositories": ["punt-labs/biff", "punt-labs/ethos"],
    "members": [
      {"identity": "claude", "role": "coo"},
      {"identity": "jmf", "role": "founder"},
      {"identity": "bwk", "role": "go-specialist"}
    ],
    "collaborations": [
      {"from": "claude", "to": "bwk", "type": "delegates_to"}
    ]
  }
]
```

Biff extracts: `members[*].identity` as the team roster.

### Where it plugs in

New function:

```python
def get_ethos_team() -> tuple[str, ...] | None:
    """Resolve team members from ethos CLI.

    Returns a tuple of identity handles, or ``None`` on any failure.
    """
```

In `_resolve_config_fields()`, the ethos team enriches
`_ConfigFields.team` when:

1. No explicit `team.members` is configured in YAML.
2. Ethos is installed and returns members.

```python
if not cf.team:
    ethos_team = get_ethos_team()
    if ethos_team is not None:
        cf = _ConfigFields(..., team=ethos_team, ...)
```

If ethos returns an empty team (repo not in any team), the result
is `()` -- same as the current zero-config behavior. The `orgs`
field is unaffected. Ethos team provides a member roster; orgs
provide repo discovery. They are orthogonal.

### What does NOT change

- `BiffConfig.orgs` -- still derived from git remote owner.
- `BiffConfig.peers` -- still from YAML config.
- Org-based repo discovery (DES-034) -- still via NATS `stream_info`.

Ethos team provides **who** is on the team. Zero-config orgs provide
**what repos** are visible. Both systems coexist.

## 3. Enhanced Presence

### BiffConfig changes

Add one field:

```python
class BiffConfig(BaseModel):
    ...
    kind: str = ""  # "human", "agent", or "" (unknown)
```

### UserSession changes

Add one field:

```python
class UserSession(BaseModel):
    ...
    kind: str = Field(
        default="",
        description="Identity kind: 'human', 'agent', or '' (unknown)",
    )
```

The `kind` field propagates through the session registration path
into the NATS KV entry, the same way `display_name` does today.

### /who output

Current:

```text
NAME         REPO              IDLE  S  P  HOST
@claude:tty1 punt-labs/biff    0s    +  +  macbook
@jmf:tty2    punt-labs/biff    3m    +  +  macbook
```

With ethos:

```text
NAME              REPO              IDLE  S  P  HOST
@claude:tty1 [A]  punt-labs/biff    0s    +  +  macbook
@jmf:tty2         punt-labs/biff    3m    +  +  macbook
```

The `[A]` tag appears after the NAME column when `kind == "agent"`.
Humans show no tag (they are the default). Unknown kind shows no tag.

Implementation: modify `_format_who_name()` in `formatting.py`:

```python
def _format_who_name(s: UserSession) -> str:
    tty = s.tty_name or (s.tty[:8] if s.tty else "")
    base = f"@{s.user}:{tty}" if tty else f"@{s.user}"
    if s.kind == "agent":
        return f"{base} [A]"
    return base
```

### /finger output

Current:

```text
>  Login: claude                         Name: Claude Agento
   Messages: on
```

With ethos:

```text
>  Login: claude [agent]                 Name: Claude Agento
   Messages: on
```

Implementation: modify `format_user_header()` in `formatting.py`:

```python
def format_user_header(session: UserSession) -> str:
    login_label = session.user
    if session.kind:
        login_label = f"{session.user} [{session.kind}]"
    left = f"Login: {login_label}"
    ...
```

### Backwards compatibility

Consumers of `/who` output parse the NAME column for `@user:tty`
addresses. The `[A]` tag is appended after the address with a space
separator. Existing parsers that split on whitespace and take the
first token get the address. The `biff.tty.parse_address()` function
already strips `@` prefixes -- it does not need to change as long
as the address precedes the tag.

The `kind` field on `UserSession` defaults to `""`. NATS KV entries
written by older biff versions will deserialize with `kind=""`.
Newer versions reading older entries see no tag. This is the correct
degradation: unknown kind shows nothing.

## 4. Edge Cases

Each edge case has a specific behavior. No code path raises an
exception when ethos is absent.

### 4.1 ethos binary not on PATH

```python
except FileNotFoundError:
    return None
```

`subprocess.run(["ethos", ...])` raises `FileNotFoundError` when the
binary is not found. Both `get_ethos_identity()` and
`get_ethos_team()` catch this and return `None`.

### 4.2 ethos installed, whoami returns exit 1

Exit 1 means ethos is installed but no identity resolves (no
`.punt-labs/ethos/identities/` files, no `iam` declaration, no
matching git config). This is a legitimate "not configured" state.

```python
if result.returncode != 0:
    return None
```

Falls through to `gh api user`.

### 4.3 ethos installed, team for-repo returns empty

Exit 0 with `[]` (empty JSON array). Repo is not in any team's
`repositories` list.

```python
teams = json.loads(result.stdout)
if not teams:
    return None  # or ()
```

Falls through to zero-config org discovery.

### 4.4 ethos returns malformed JSON

```python
except json.JSONDecodeError:
    return None
```

Catches both invalid JSON and unexpected structure. Falls through
to the next resolution step.

### 4.5 ethos call takes >2s

```python
except subprocess.TimeoutExpired:
    return None
```

The `timeout=2` parameter on `subprocess.run` kills the process
after 2 seconds. Falls through to `gh api user`. The user sees a
~2s startup delay in the worst case, which is acceptable for a
one-time resolution at server start.

### 4.6 ethos identity has no display_name

The `name` field in ethos is required by validation, but defensive
code handles it anyway:

```python
display_name = data.get("name", "") or handle
```

If `name` is empty or absent, use `handle` as the display name.
This mirrors the current behavior where `gh api user` can return
an empty name.

### 4.7 Multiple teams match the repo

`ethos team for-repo --json` can return multiple teams. Biff takes
the union of all members across all matching teams:

```python
all_members: set[str] = set()
for team in teams:
    for member in team.get("members", []):
        if isinstance(member, dict) and "identity" in member:
            all_members.add(member["identity"])
return tuple(sorted(all_members))
```

### 4.8 ethos identity has kind other than "human" or "agent"

The `kind` field is stored as-is. The `/who` formatter only checks
for `"agent"` to show `[A]`. The `/finger` formatter shows whatever
`kind` contains in brackets. An unexpected kind value (e.g., `"bot"`)
shows `[bot]` in finger and no tag in who. This is correct: no
crash, no special casing.

## 5. Startup Cost

Both ethos calls happen in `load_config()`, which runs once at
server start. The cost:

| Call | Expected latency | Timeout |
|------|-----------------|---------|
| `ethos whoami --json` | ~10ms | 2s |
| `ethos team for-repo --json` | ~15ms | 2s |
| `gh api user` (existing) | ~200ms | none (currently) |

If ethos resolves identity, the `gh api user` call is skipped.
Net effect: startup is ~190ms faster when ethos is installed, because
a local binary replaces a GitHub API round-trip.

If ethos is absent, the two failed subprocess spawns add ~2ms total
(FileNotFoundError is fast). Negligible.

## 6. Testing

### Unit tests

- `test_get_ethos_identity_success` -- mock subprocess, return valid JSON.
- `test_get_ethos_identity_not_installed` -- mock FileNotFoundError.
- `test_get_ethos_identity_exit_1` -- mock returncode 1.
- `test_get_ethos_identity_timeout` -- mock TimeoutExpired.
- `test_get_ethos_identity_bad_json` -- mock invalid JSON on stdout.
- `test_get_ethos_identity_missing_handle` -- mock JSON without `handle`.
- `test_get_ethos_identity_missing_name` -- mock JSON with empty `name`.
- `test_get_ethos_team_success` -- mock subprocess, return team JSON.
- `test_get_ethos_team_empty` -- mock empty array.
- `test_get_ethos_team_not_installed` -- mock FileNotFoundError.
- `test_get_ethos_team_multi_team` -- mock two teams, verify member union.

### Integration tests

- `test_load_config_with_ethos` -- mock ethos subprocess, verify
  `BiffConfig.user` and `BiffConfig.kind` are set from ethos.
- `test_load_config_ethos_fallback_to_gh` -- mock ethos failure,
  verify `gh api user` path still works.
- `test_load_config_ethos_absent` -- mock FileNotFoundError for both
  ethos and gh, verify OS username fallback.

### Presence tests

- `test_who_shows_agent_tag` -- create session with `kind="agent"`,
  verify `[A]` in output.
- `test_who_no_tag_for_human` -- create session with `kind="human"`,
  verify no tag.
- `test_who_no_tag_for_unknown` -- create session with `kind=""`,
  verify no tag.
- `test_finger_shows_kind` -- verify `[agent]` in finger header.

## 7. Files Changed

| File | Change |
|------|--------|
| `src/biff/config.py` | Add `EthosIdentity`, `get_ethos_identity()`, `get_ethos_team()`. Modify `load_config()` identity resolution block. |
| `src/biff/models.py` | Add `kind` field to `BiffConfig` and `UserSession`. |
| `src/biff/formatting.py` | Modify `_format_who_name()` and `format_user_header()` for kind tags. |
| `src/biff/server/tools/_session.py` | Pass `kind` through session registration. |
| `tests/test_server/test_config.py` | Add unit tests for ethos functions. |
| `tests/test_server/test_formatting.py` | Add presence tag tests. |
| `tests/test_integration/` | Add ethos fallback integration tests. |
