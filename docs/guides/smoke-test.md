# Post-Release Smoke Test

Manual verification for biff after release, install, or MCP server changes.
Exercises every MCP tool and CLI command to confirm team messaging,
presence, named CLI sessions, and identity display work end-to-end.

Phase 1 (MCP tools) runs from a Claude Code session in a biff-enabled repo.
Phase 2 (CLI) and Phase 3 (named sessions) run from a plain terminal — a
Claude Code session sets `CLAUDE_CODE_SESSION_ID`, which takes the harness
resolution rung and masks the CLI session store. To exercise the CLI session
surface from inside a Claude Code shell, prefix each command with
`CLAUDE_CODE_SESSION_ID=""`.

The relay is shared and live; `who` will show other real sessions across
repos and hosts. That is expected — verify your own rows, not an empty board.

## Prerequisites

Installed, enabled, version correct:

```bash
biff --version          # biff X.Y.Z (plugin X.Y.Z) — both channels match
biff status             # relay reachable, this session's key and tty shown
```

Expected: `--version` prints the released package version and, after a
`claude plugin update` + restart, the same plugin version. A mismatch
(`plugin 1.22.0` while the package is `1.23.0`) means Claude Code has not
restarted onto the new plugin yet.

## Phase 1: MCP Tools

Run each from a Claude Code session via its slash command or MCP tool. Emit
biff output verbatim.

### 1.1 who

**Tool:** `who`

**Verify:** A table with columns `NAME K MGR REPO IDLE S P HOST`. This
session's row is present with kind `[A]` and the `MGR` column naming the
addressable human behind the agent (blank for human rows; column hidden when
no row has a value).

### 1.2 finger — self

**Tool:** `finger` (`user: claude`)

**Verify:** Login line with name and kind, `Messages: on`, a `Role:` line
(`coo (engineering) — reports to ceo (jfreeman)` when ethos team data is
present), `Launched by: <os user>`, host, dir, and the current plan.

### 1.3 plan (write path)

**Tool:** `plan` (`message: smoke-test NNNN`)

**Verify:** Returns `Plan:`. A following `finger claude` shows the new plan
text. A message over 512 characters is rejected, naming the length and the
limit, with the existing plan unchanged.

### 1.4 Plan gate unlocks edits

After 1.3, perform any `Edit`/`Write` in the session.

**Verify:** The edit is allowed — the plan-gate marker keyed on this Claude
Code session (`CLAUDE_CODE_SESSION_ID`) unlocks it. A blocked edit here means
the gate is keying on the wrong id.

### 1.5 write + read round-trip

**Tool:** `write` (`to: claude:<your-tty>`, `message: smoke self RT`), then
`read_messages`.

**Verify:** `write` returns `sent`. The unread nudge fires. `read_messages`
returns the message from your own tty. A second `read_messages` returns
`no new messages`.

### 1.6 write — stale tty forward (DES-077)

**Tool:** `write` (`to: <a-known-user>:deadtty999`, `message: smoke forward`)

**Verify:** Returns a forward notice (`deadtty999 has no active session;
delivered to <user>'s mailbox`), not a bounce. The message lands in that
user's mailbox in a repo where they are live (confirm with that user, or via
`biff --user <user> status` on this host if it is your own identity).

### 1.7 write — unknown user rejected

**Tool:** `write` (`to: nosuchuser:x`, `message: smoke`)

**Verify:** Errors with `user nosuchuser not found in visible repos`. Nothing
is sent. A silent accept is a FAIL — a message must never reach a user the
sender did not name.

### 1.8 wall

**Tool:** `wall` (`message: smoke wall NNNN`)

**Verify:** Returns confirmation. A teammate's `who`/statusline shows the
wall item (bold red on statusline line 2). `wall` with no message reads the
current wall; clearing removes it.

### 1.9 mesg

**Tool:** `mesg` (`enabled: false`, then `enabled: true`)

**Verify:** With mesg off, `biff statusline` renders the biff segment as the
literal `(n)` shape (count hidden, not `0`). With mesg on, it renders
`user:tty(N)`. (The MCP tool takes `enabled: bool`; the CLI takes `on`/`off`.)

### 1.10 tty (rename)

**Tool:** `tty` (`name: smoke-renamed`)

**Verify:** `who` shows this session under the new name with no orphaned old
row (the rename updates the session record, not a second entry).

### 1.11 last

**Tool:** `last`

**Verify:** Session login/logout history, most recent first, with user, tty,
and timestamps.

### 1.12 talk (invite + read)

**Tool:** `talk` (`to: <a-live-peer>:<tty>`, optional `message:`). The peer
calls `talk_read`.

**Verify:** The peer's `talk` tool description gains a leading `[TALK]` with
`wants to talk`; `talk_read` surfaces the invite and any message. `talk_end`
closes the session. Self-talk (`to:` your own key) errors with
`You can't talk to your own session`. SKIP if no live peer is available.

### 1.13 poll config

**Tool:** `set_poll_interval` (`interval: 1m`), then `get_poll_status`.

**Verify:** `get_poll_status` reports interval `1m`, active, and a last-check
time. `set_poll_interval` with `n` disables it.

## Phase 2: CLI Commands

Run from a terminal on the host where biff is installed. Inside a Claude Code
shell, prefix each with `CLAUDE_CODE_SESSION_ID=""` so the CLI session store
is exercised rather than the harness rung.

### 2.1 version

```bash
biff --version
```

**Verify:** Matches the released version; `(plugin X.Y.Z)` present when a
plugin is installed, computed, not hard-coded.

### 2.2 status

```bash
biff status
```

**Verify:** Relay URL, resolved user, session key, tty name, and unread count.

### 2.3 who

```bash
biff who
```

**Verify:** The presence table renders. Run it twice back-to-back from the
same shell — the row keeps a **stable** tty across both invocations (the
named-session identity; before PL-245 a bare CLI invocation drew a fresh tty
each time). This is the headline CLI canary.

### 2.4 finger

```bash
biff finger claude
```

**Verify:** Same content as 1.2 — role chain, launched-by, plan.

### 2.5 plan

```bash
biff plan "CLI smoke NNNN"
```

**Verify:** `biff finger claude` shows the new plan.

### 2.6 write + read

```bash
biff write claude:<your-tty> "cli smoke RT"
biff read
```

**Verify:** `write` prints `sent`; `read` shows the message then marks it
read. A second `read` shows none.

### 2.7 wall

```bash
biff wall "cli smoke wall"
biff wall
```

**Verify:** First posts; bare `biff wall` reads it back.

### 2.8 mesg

```bash
biff mesg n
biff statusline < /dev/null
biff mesg y
```

**Verify:** Segment is the literal `(n)` shape while off, `user:tty(N)` while
on.

### 2.9 last

```bash
biff last
```

**Verify:** Login/logout history.

### 2.10 statusline

```bash
biff statusline < /dev/null
```

**Verify:** Two lines — line 1 ends with the biff segment `user:tty(N)`
(plain when `0`, bold yellow when nonzero); line 2 is the idle marker `▶`
alone, or `▶ <text>` for a wall (bold red) or talk (bold yellow) item.

## Phase 3: Named CLI Sessions (PL-245 / PL-246)

Run from a plain terminal (or prefix `CLAUDE_CODE_SESSION_ID=""`). These
exercise the CLI session store, which the harness rung otherwise masks.

### 3.1 session new

```bash
biff session new --tty smoketest
```

**Verify:** Prints `smoketest`. Becomes this repo's default session.

### 3.2 session ls

```bash
biff session ls
```

**Verify:** One row: `NAME USER REPO STATE LAST-ATTACHED ID`, state `live`,
id prefixed `cli-`.

### 3.3 Resolution rung — explicit `-s`

```bash
biff -s smoketest who
biff -s smoketest who
```

**Verify:** Both calls render `who` as `<user>:smoketest`, stable across both.

### 3.4 Resolution rung — `BIFF_SESSION`

```bash
BIFF_SESSION=smoketest biff who
```

**Verify:** Resolves to the `smoketest` session.

### 3.5 Resolution rung — per-repo default

```bash
biff who
```

**Verify:** Bare `who` reuses `smoketest` (the default set by 3.1), same tty.

### 3.6 Unknown `-s` errors, never auto-creates

```bash
biff -s no-such-session who; echo "exit=$?"
```

**Verify:** Exit 1, `Error: unknown session: no-such-session`. No new session
is created (`biff session ls` unchanged). `-s` never creates.

### 3.7 session use

```bash
biff session new --tty other
biff session use smoketest
biff who
```

**Verify:** `use` makes `smoketest` the repo default without attaching; bare
`who` resolves to it.

### 3.8 session end + inbox drain

Queue mail to the session first (from another shell or peer:
`biff write <user>:smoketest "drain me"`), then:

```bash
biff session end smoketest
biff read
```

**Verify:** `end` prints `ended smoketest`; the queued message has moved to
your user mailbox (`biff read` shows it). `biff session ls` no longer lists
`smoketest`.

### 3.9 Ended session via `-s` errors

```bash
biff -s smoketest who; echo "exit=$?"
```

**Verify:** Exit 1, naming the end time and suggesting
`biff session new --tty smoketest`.

### 3.10 Cleanup

```bash
biff session end other 2>/dev/null; biff session ls
```

**Verify:** `No sessions.` (or only sessions you intend to keep).

## Phase 4: Identity & Display (DES-076)

### 4.1 MGR column

```bash
biff who
```

**Verify:** The `MGR` column names the biff-addressable human behind each
agent row (first non-empty of `git config user.name` and the OS user that
differs from the agent's own identity). Human rows blank; column hidden when
no row has a value. Needs no ethos data.

### 4.2 finger role chain + launched-by

```bash
biff finger claude
```

**Verify:** `Role:`/`Roles:` line(s) present when the repo has ethos team
data, each `<role> (<team>) — reports to <role> (<person>)`; `Launched by:
<os user>` present. A repo with no team data omits the role lines and the
rest of the output is unchanged.

### 4.3 Standalone identity (no ethos CLI)

**Verify:** 4.1 and 4.2 produce their output with no `ethos` subprocess
spawned (identity resolves from `.punt-labs/ethos/identities/claude.yaml` on
disk, DES-040). A missing or broken ethos binary must not change the result.

## Phase 5: Install Verification

Only after a fresh install or re-install.

### 5.1 Binaries on PATH

```bash
which biff biff-hook
```

**Verify:** Both resolve to `~/.local/bin` (the uv tool install location).

### 5.2 Plugin installed

```bash
claude plugin list | grep biff
```

**Verify:** `biff@punt-labs` installed, user scope.

### 5.3 doctor / enablement

```bash
biff status
```

**Verify:** `Enabled: yes (.punt-labs/biff/enabled)`, relay reachable, status
line configured, commands present. Git hooks may show missing until
`biff enable` runs in the repo — that is expected, not a failure.

### 5.4 Relay reachable

```bash
biff status
```

**Verify:** The relay URL (`biff-relay.punt-labs.com:4222`) is reported
reachable. A connection error here is a hard FAIL.

## Quick Pass Criteria

- Phase 1: every MCP tool succeeds; stale-tty forward (1.6) forwards, unknown
  user (1.7) is rejected, plan gate (1.4) unlocks, mesg-off shape (1.9) is
  the literal `(n)`.
- Phase 2: every CLI command succeeds; the back-to-back `who` (2.3) keeps a
  stable tty.
- Phase 3: the full session lifecycle works; unknown `-s` (3.6) and ended `-s`
  (3.9) both exit 1 without creating anything; the end-drain (3.8) moves mail
  to the user mailbox.
- Phase 4: MGR column, role chain, and launched-by render; identity resolves
  with no ethos subprocess.
- Phase 5: binaries on PATH, plugin installed, relay reachable.

## Quick Fail Indicators

- Back-to-back `biff who` (2.3) draws a different tty each call — the CLI
  session store is not resolving; named-session identity broken.
- `--version` shows a plugin version behind the package after a restart —
  Claude Code did not reload onto the new plugin, or the marketplace ref did
  not advance.
- `write` to an unknown user (1.7) returns `sent` — the recipient-visibility
  gate is off; messages can leak to unnamed users.
- Stale-tty `write` (1.6) bounces instead of forwarding — DES-077 regression.
- Plan set, but the next edit is still blocked (1.4) — the plan-gate marker
  is keying on the wrong session id.
- `-s <unknown>` creates a session (3.6) — `-s` must never auto-create.
- mesg-off statusline shows `(0)` instead of `(n)` (1.9/2.8) — count-hiding
  broken; mail delivery to a muted session would silently stop.
- `finger` spawns an `ethos` subprocess (4.3) — identity is not resolving
  from disk.

## Report Format

Include in the release recap email.

```text
Biff Smoke Test Report
======================
Version:  <version>  (plugin <version>)
Host:     <hostname>
Date:     <YYYY-MM-DD HH:MM>
Stack:    <prod plugin / dev --plugin-dir>

Phase 1: MCP Tools
  1.1  who                        PASS / FAIL
  1.2  finger (self)              PASS / FAIL
  1.3  plan (write + cap)         PASS / FAIL
  1.4  plan gate unlocks edit     PASS / FAIL
  1.5  write + read round-trip    PASS / FAIL
  1.6  write stale-tty forward    PASS / FAIL  ← DES-077 canary
  1.7  write unknown user → error PASS / FAIL  ← visibility gate
  1.8  wall                       PASS / FAIL
  1.9  mesg (off → (n) shape)     PASS / FAIL
  1.10 tty rename                 PASS / FAIL
  1.11 last                       PASS / FAIL
  1.12 talk                       PASS / FAIL / SKIP
  1.13 poll config                PASS / FAIL

Phase 2: CLI
  2.1  version                    PASS / FAIL  <version string>
  2.2  status                     PASS / FAIL
  2.3  who (stable tty x2)        PASS / FAIL  ← CLI session canary
  2.4  finger                     PASS / FAIL
  2.5  plan                       PASS / FAIL
  2.6  write + read               PASS / FAIL
  2.7  wall                       PASS / FAIL
  2.8  mesg                       PASS / FAIL
  2.9  last                       PASS / FAIL
  2.10 statusline                 PASS / FAIL

Phase 3: Named CLI Sessions
  3.1  session new                PASS / FAIL
  3.2  session ls                 PASS / FAIL
  3.3  rung: -s                   PASS / FAIL
  3.4  rung: BIFF_SESSION         PASS / FAIL
  3.5  rung: repo default         PASS / FAIL
  3.6  unknown -s → exit 1        PASS / FAIL  ← never auto-creates
  3.7  session use                PASS / FAIL
  3.8  session end + drain        PASS / FAIL
  3.9  ended -s → exit 1          PASS / FAIL
  3.10 cleanup                    PASS / FAIL

Phase 4: Identity & Display
  4.1  MGR column                 PASS / FAIL
  4.2  role chain + launched-by   PASS / FAIL
  4.3  standalone (no ethos CLI)  PASS / FAIL

Phase 5: Install
  5.1  binaries on PATH           PASS / FAIL
  5.2  plugin installed           PASS / FAIL
  5.3  enabled / status           PASS / FAIL
  5.4  relay reachable            PASS / FAIL

Result: PASS / FAIL
  Passed: <count>
  Failed: <count>
  Skipped: <count>
  Notes: <observations, warnings, follow-up issues created>
```

SKIP is valid for: 1.12 (no live talk peer). Everything else must be PASS or
FAIL with explanation.
