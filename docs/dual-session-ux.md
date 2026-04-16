# Design: Dual-Session UX

**Bead:** biff-uw6i
**Date:** 2026-04-16
**Status:** PROPOSED
**Related:** DES-039 (dual-session), `docs/dual-session.md` (session model)

## Summary

Two UX changes for dual-session mode:

1. The status bar shows the **human** (root) identity, not the agent
   (primary).
2. `/read` groups messages by identity when dual-session is active.

Both changes are display-only. No changes to session registration,
message delivery, or heartbeat logic.

## 1. Current State

### Status Bar (Unread File)

`_sync_unread_file` in `_descriptions.py` writes a JSON file consumed
by `biff statusline`. The `user` field is always `state.config.user`,
which is the **primary** identity (the agent in dual-session):

```python
_write_unread_file(
    state.unread_path,
    summary,
    repo_name=state.config.repo_name,
    user=state.config.user,         # <-- always primary (e.g. "claude")
    tty_name=_tty_name,
    biff_enabled=_biff_enabled,
    display_items=items,
    plan=plan,
)
```

`_biff_segment` in `statusline.py` reads `unread.user` and renders:

```text
claude:tty4(2)
```

The human at the terminal sees the agent's identity. This is wrong --
the status bar is a human-facing display surface.

### read_messages

`read_messages` in `messaging.py` fetches from all four inboxes
(primary tty, primary user, companion tty, companion user), merges
chronologically, and passes the flat list to `format_read`:

```python
all_unread = sorted(
    tty_unread + user_unread + comp_tty + comp_user,
    key=lambda m: m.timestamp,
)
# ...
return format_read(all_unread)
```

`format_read` in `formatting.py` renders a single table:

```text
FROM         DATE              MESSAGE
kai:tty2     Mon Apr 14 15:30  hey, review PR #205?
rmh:tty3     Mon Apr 14 15:45  implementation done
```

No indication of which identity each message was addressed to.

## 2. Proposed Changes

### Change 1: Status Bar Shows Human Identity

**File:** `src/biff/server/tools/_descriptions.py`
**Function:** `_sync_unread_file`

When `state.companion` is not `None`, write the companion (root/human)
identity to the unread file instead of the primary (agent) identity.
The companion's `tty_name` is also used so the status bar address is
copy-pasteable for the human.

```python
if state.companion is not None:
    status_user = state.companion.user
    status_tty = state.companion.tty_name
else:
    status_user = state.config.user
    status_tty = _tty_name
```

Pass `status_user` and `status_tty` to `_write_unread_file`.

No changes to `_write_unread_file` itself -- its `user` and `tty_name`
parameters already accept arbitrary values.

No changes to `statusline.py` -- it reads whatever `user` and
`tty_name` the unread file contains.

**Rationale:** The status bar is rendered in the human's terminal.
The human identity is what the human expects to see and what other
users should use when addressing them directly.

### Change 2: read_messages Sections Per Identity

**File:** `src/biff/server/tools/messaging.py`
**Function:** `read_messages`

**File:** `src/biff/formatting.py`
**Function:** new `format_read_dual`

When dual-session is active and at least one message exists, group
messages by target identity and render with section headers.

#### Message Attribution

Each message knows its destination via `m.to_user`. Messages from the
four inboxes are already separated by fetch:

| Source | Identity |
|--------|----------|
| `tty_unread` (primary tty inbox) | primary (agent) |
| `user_unread` (primary user inbox) | primary (agent) |
| `comp_tty` (companion tty inbox) | companion (human) |
| `comp_user` (companion user inbox) | companion (human) |

Instead of merging all four lists into one flat list, partition into
two groups based on which inbox they came from:

```python
human_msgs = sorted(comp_tty + comp_user, key=lambda m: m.timestamp)
agent_msgs = sorted(tty_unread + user_unread, key=lambda m: m.timestamp)
```

#### Formatting

New function `format_read_dual` in `formatting.py`:

```python
def format_read_dual(
    human_user: str,
    human_msgs: list[Message],
    agent_user: str,
    agent_msgs: list[Message],
) -> str:
    """Format messages with per-identity section headers."""
```

Each non-empty section gets a `"^  {user}"` header line (using the
existing `_HEADER_PREFIX` convention from `_formatting.py`) followed
by the column headers and message rows indented under it.

Human section is listed first. Within each section, messages are
sorted chronologically (already sorted by the caller).

#### Call Site

In `read_messages`, after fetching and before formatting:

```python
if state.companion is not None and all_unread:
    human_msgs = sorted(comp_tty + comp_user, key=lambda m: m.timestamp)
    agent_msgs = sorted(tty_unread + user_unread, key=lambda m: m.timestamp)
    return format_read_dual(
        state.companion.user, human_msgs,
        state.config.user, agent_msgs,
    )
return format_read(all_unread)
```

Single-session path is unchanged -- `format_read` is called as before.

## 3. Edge Cases

### Single-Session (No Companion)

`state.companion is None`. Both changes are gated on companion
presence:

- Status bar: `status_user = state.config.user` (unchanged).
- read_messages: falls through to `format_read` (unchanged).

Zero behavioral change for single-session users.

### Only One Identity Has Messages

`format_read_dual` only renders sections that have messages. If only
the human has messages, only the human section appears. If only the
agent has messages, only the agent section appears.

When a section is omitted, no header is printed for it. The output
looks like a single section with header, not like the single-session
format (which has no header). This is intentional -- the header
signals "dual-session is active" even when one inbox is empty.

### Both Inboxes Empty

The early return in `read_messages` fires before any formatting:

```python
if not all_unread:
    return "No new messages."
```

This path is identical in single-session and dual-session. No change.

### Companion Registered Late

The companion may be `None` at first `read_messages` call if ethos
roster was not available at startup (late registration via
`_try_late_companion_registration`). In this window:

- Status bar shows the primary identity (existing behavior).
- `read_messages` uses the single-session format.

After late registration succeeds, the next poll tick writes the
companion identity to the unread file, and subsequent `read_messages`
calls use the dual format. No special handling needed -- the `None`
guard covers it naturally.

### Companion Inbox Messages Arrive Before Registration

Not possible. The companion session key does not exist in NATS KV
until `_register_companion` succeeds. No subject to deliver to.

## 4. Mockups

### (a) Status Bar With Human Identity (Dual-Session)

```text
biff:main | 23% | $0.45 | jfreeman:tty3(2)
^  rmh: implementation done, ready for review
```

Previously showed `claude:tty4(2)`. Now shows `jfreeman:tty3(2)` --
the human's identity and TTY name.

### (b) read_messages With Both Sections

```text
^  jfreeman
   FROM         DATE              MESSAGE
   kai:tty2     Mon Apr 14 15:30  hey Jim, review PR #205?
   ada:tty7     Mon Apr 14 15:32  CI is green on punt-kit

^  claude
   FROM         DATE              MESSAGE
   rmh:tty3     Mon Apr 14 15:45  implementation done, ready for review
   bwk:tty5     Mon Apr 14 15:48  go module updated
```

Human section listed first. Each section has its own header and
column headers. Messages sorted chronologically within each section.

### (c) read_messages With Only Human Messages

```text
^  jfreeman
   FROM         DATE              MESSAGE
   kai:tty2     Mon Apr 14 15:30  hey Jim, review PR #205?
```

Only the section with messages appears. No empty `claude` section.

### (d) Single-Session (Unchanged)

```text
   FROM         DATE              MESSAGE
   kai:tty2     Mon Apr 14 15:30  hey, review PR #205?
   rmh:tty3     Mon Apr 14 15:45  implementation done
```

No section headers. Identical to current output. The `format_read`
path is taken when `state.companion is None`.

## 5. Files Changed

| File | Change |
|------|--------|
| `src/biff/server/tools/_descriptions.py` | `_sync_unread_file`: use companion identity for status bar user/tty |
| `src/biff/server/tools/messaging.py` | `read_messages`: partition messages, call `format_read_dual` |
| `src/biff/formatting.py` | Add `format_read_dual` |
| `tests/test_server/test_formatting.py` | Tests for `format_read_dual` |
| `tests/test_integration/test_dual_session_ux.py` | Integration tests for both changes |

## 6. Non-Changes

- `statusline.py` -- reads whatever the unread file contains. No code change.
- `unread.py` -- data model unchanged. `SessionUnread.user` is already a string.
- `_write_unread_file` -- signature unchanged. Already accepts `user` and `tty_name` as parameters.
- `_biff_segment` -- reads `unread.user`. Works with any identity string.
- `format_read` -- unchanged. Still used for single-session.
- Session registration, heartbeat, cleanup -- unchanged.
- Message delivery routing -- unchanged.
