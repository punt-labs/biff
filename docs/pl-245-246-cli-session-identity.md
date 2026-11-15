# CLI session identity and stable addressing

Status: DRAFT for leader review and operator ratification. Design only; no
implementation is dispatched until the open decisions in section 7 are ruled.

Read before writing this document: DESIGN.md DES-064 (harness-declared caller
identity), DES-050 (route on a durable session id; `derive_routing_id`),
DES-040 (agent identity from disk), DES-076 (the four identity sources and the
addressable-display rule), DES-035 (tty name reservation);
`punt-kit/standards/mcp-caller-identity.md` section 4.2 (get-or-create, live,
dormant, ended, resume); `docs/session-model.tex` (the `ttyalias` injection,
`ResumeSession`, `DeliverIdentity`/`DeliverAliasLost`/`DeliverAliasMis`);
`src/biff/session_routing.py`, `src/biff/cli_session.py`,
`src/biff/commands/write.py`, plus `resolve_tty_name`
(`src/biff/server/tools/_session.py`), `commands/plan.py`, `commands/read.py`,
and the relay's two inbox kinds (`relay.py`, `nats_relay.py`).

## 1. Problem

The MCP path has a stable, harness-declared identity: the caller states a
`session_id`, and `SessionRouting` derives the routing id, the relay key, and
the unread path from it (DES-064, DES-050). The CLI path has none of this.

1. `cli_session()` calls `generate_tty()` on every `biff <cmd>`. Two commands
   from one shell are two different sessions, each registered, each given a new
   `ttyN`, each deleted on exit. Nothing a CLI invocation does is attributable
   to a durable session, and the plan gate has no CLI identity to key on.
2. A `user:ttyN` address is a display alias. When the owner's session restarts,
   the alias stops naming anything, and `biff write` refuses with "No active
   session". The sender has to run `who` and retype the address.

The two are coupled. Stable addressing needs something stable to address; a
named, durable CLI session is that thing.

## 2. Principles carried over (settled, not revisited)

- Route on a globally unique id. Repo and org are visibility, not routing
  coordinates (DES-050; the routing-id principle).
- A display alias is a *name that currently has a holder*, resolved at send
  time. Recycling an alias onto a new holder is by design. The defect is only
  the alias that has no holder.
- Caller identity is stated, not inferred (DES-064). The CLI may read the
  harness-declared session from `CLAUDE_CODE_SESSION_ID` (already done by
  `cli_session_id()` for the plan marker), nothing else about the process.
- Identity (the user name) is resolved once per session from the DES-076
  human chain: `--user` override, GitHub login, OS user. No agent step.

## 3. The CLI session model

### 3.1 Session and invocation are separate

A **session** is a first-class, named record with its own lifecycle. An
**invocation** is one `biff <cmd>` process. An invocation *attaches* to a
session, runs, and detaches. Detaching does not end the session and does not
delete its relay row.

This is the same separation the engine-session lifecycle already makes for the
harness (standard section 4.2): durable state belongs to the session; live
resources belong to the run that registered them. For a CLI session the "run"
is the invocation (or the REPL process, which also heartbeats).

### 3.2 The session record

One JSON file per session at
`~/.punt-labs/biff/sessions/<session_id>/session.json`, mode `0600`, in
directories of mode `0700`, written through a temporary file renamed into
place (the same discipline as the harness `handle` file). The `session_id`
charset is `[A-Za-z0-9_-]+`, checked with the existing `ID_RE` before any path
is built.

| Field | Meaning |
|---|---|
| `version` | Record schema version, an integer. |
| `session_id` | Opaque id, `cli-` plus 16 hex characters, minted once. Never reused. |
| `tty` | The display name (`tty3`, or the operator's `--tty` choice). The only handle a person types. |
| `user` | The identity, resolved once at creation and frozen. |
| `repo` | Repo name (the visibility coordinate) and resolved repo root. Frozen. |
| `hostname` | Host the record lives on. |
| `created_at` | UTC creation time. Never changes. |
| `state` | `live`, `dormant`, or `ended`. |
| `last_attached_at` | UTC time of the last attach; the lease clock. |
| `ended_at` | Set when `state` becomes `ended`. |

The tuple the formal model treats as first-class state is
`(session_id, tty, user, repo, created_at)`. The first four never change after
creation; only `state`, `last_attached_at`, and `ended_at` do.

The `cli-` prefix is reserved: harness session ids are not minted by biff and
never carry it, so a CLI id cannot collide with a Claude Code id in the shared
`sessions/` directory. Routing ids are also domain-separated (section 3.7).

### 3.3 Name versus id

The operator's preference is that the tty name *be* the session id, with no
second namespace. The design keeps one namespace **in what people type** but
not in what the system routes on, and the reason is a defect already on record.

`ttyN` is assigned lowest-free and is recycled after release (DES-035). If the
tty name were the routing key, then after `end` released `tty3`, a new session
would claim `tty3`, derive the same routing id, and inherit the dead session's
inbox. That is the exact stranded-or-misrouted defect DES-050 removed for
harness sessions. A routing coordinate must never be a recyclable name.

So the record has an opaque `session_id` that routing uses and a `tty` name that
is the alias. The operator-visible rule is still "one namespace": every verb
that takes a session (`-s`, `use`, `end`) accepts the **name**, and accepts the
id as an exact alternative. `session ls` prints the name first and the id in a
trailing column and in `--json`. Nobody needs the id unless two sessions on one
host share a name (two users on one machine), in which case the id
disambiguates.

This is Open decision 1.

### 3.4 Verbs

```text
biff session new [--tty <name>] [--repo <path>] [--as <user>]
biff -s <name|id> <cmd> ...
biff session ls [--all] [--json]
biff session use <name|id>
biff session end <name|id>
```

**`session new`.** Creates a record in state `live`, reserves the tty name,
registers the relay row, writes the host pointer (section 3.5), and prints the
name. `--tty` asks for that exact name; if the reservation fails the command
errors and creates nothing (no silent fallback to `ttyN`). Without `--tty` the
name comes from `claim_tty_name` as today. `--repo` defaults to the enclosing
git repo of the current directory and is resolved to an absolute root. `--as`
overrides the identity chain for this session only.

**`biff -s <name|id> <cmd>`.** `-s` is a global option, with the `BIFF_SESSION`
environment variable as its equivalent. The invocation attaches to that session
(section 3.6) and runs `<cmd>` as it. An unknown name or id, or an ended
session, is an error with a named message; an explicit `-s` never auto-creates.

**`session ls`.** One row per non-ended session on this host: name, user, repo,
state, last attached, and id. `--all` includes ended sessions still inside the
retention window.

**`session use <name|id>`.** Makes the session the default for this repo
(writes the pointer). It does not attach and does not change state.

**`session end <name|id>`.** Ends the session (section 3.8).

### 3.5 Zero-config default

With no `-s` the invocation resolves its session by the first rung that yields
a usable session:

1. `-s <name|id>` (explicit).
2. `BIFF_SESSION` in the environment (explicit; same validation).
3. `CLAUDE_CODE_SESSION_ID`, when present and well-formed: the harness has
   declared the session, so the CLI is a client of the *engine session* the MCP
   server already registered. It attaches to that session's relay row and
   routing id (derived with the harness role) and creates no CLI record and no
   second presence row. This is Open decision 2.
4. The host pointer: `~/.punt-labs/biff/current/<repo_key>`, a file holding one
   session id, where `repo_key` is a 16-hex digest of the resolved repo root.
5. Auto-create: the equivalent of `session new` with defaults, then write the
   pointer.

Rungs 1 and 2 are explicit and error when they cannot be honored. Rungs 3 to 5
are implicit and never error on a stale target: a pointer naming an ended,
expired, missing, wrong-repo, or wrong-host record is ignored, and the
invocation falls through to auto-create, which rewrites the pointer.

The pointer is keyed per repo, not one global file as first sketched, because a
single global pointer would attach `biff who` in repo B to a session created in
repo A. The record's frozen `repo` is what makes the check possible.

Effect on today's behavior: a shell that runs `biff who`, then `biff write`,
then `biff plan` is now one session with one `ttyN`. Between commands the
session is idle, so `/who` (which shows only rows renewed within the liveness
window) stops listing it after that window, as today's deleted row does. The
difference is that its address and inbox survive.

### 3.6 Attach, lease, and the relay row

An attach does these things, in order:

1. Load and validate the record (state not `ended`; `user` and `repo` match
   what the invocation implies; `-s` plus a conflicting `--user` is an error,
   not an override).
2. If the state is `dormant`, set it `live` (revival; the same session).
3. Renew the relay row (`last_active`) and set `last_attached_at`.
4. Run the command.
5. On exit do **not** delete the row and do **not** release the tty name.

Interactive mode (the REPL) additionally runs the existing heartbeat loop while
attached. A one-shot invocation cannot heartbeat, so a session with no
attached process lapses by lease: after the lease period with no attach the
record moves `live` to `dormant`. Dormant is "the same run being quiet": the
tty name stays reserved, the inbox keeps accepting messages, and the next
attach revives it with nothing re-established. The lease is measured in hours
(abandonment, not activity), and retention matches the existing tty-name
reservation lifetime of three days.

A dormant or live session is a holder of its alias. Alias resolution at send
time therefore considers rows inside the relay's storage lifetime, not only
rows inside the 120 second `/who` liveness window. The implementation must
confirm that `get_sessions_for_repos` returns those rows; if it filters by
liveness, the write path needs the storage-lifetime view.

### 3.7 Identity and routing

The identity is resolved once, at `new`, by the DES-076 human chain with `--as`
as the override, and frozen into the record. It is not re-resolved per
invocation, because a session whose user changes between commands is two
sessions. `--user` on a command that attaches via `-s` or the pointer must
equal the record's user or the command errors.

`SessionRouting` today hardcodes the harness role. It takes the role as a
parameter, with two constants in `session_id.py`: the existing harness role and
a CLI role (`biff.cli.session.v1`). The routing id of a CLI session is
`derive_routing_id(session_id, CLI_ROLE)`, so the relay key is
`{user}:{routing_id}` exactly as for harness sessions, and a CLI id that
somehow equalled a harness id would still derive a different routing id.
`unread_path`, the relay row, and the plan marker all go through the same
`SessionRouting` object, which keeps the "one derivation" property the module
exists for.

Rung 3 of the default chain uses the harness role, so a CLI command run inside
Claude Code and the MCP server in that same session agree on the key.

### 3.8 Transition rules

| Operation | From | Result |
|---|---|---|
| `new` | (absent) | New record, `live`. Name reserved. A fresh `session_id` every time, even for a reused name. |
| attach | `live` | Renew lease. |
| attach | `dormant` | Revive: `live`, same session, same inbox. |
| attach, `-s`/`use` | `ended` | Error: `session <name> ended at <time>; run biff session new --tty <name>`. Never revives. |
| attach, `-s`/`use` | absent/expired | Error: unknown session. |
| lease lapse | `live` | `dormant`. Nothing released. |
| `end` | `live` or `dormant` | `ended`; see below. |
| retention lapse | `dormant` or `ended` | Record removed; name released; pointer entries naming it are stale. |

**`use` on an ended or expired session** is an error, not a silent new session.
`use` is an explicit act naming a specific session; substituting another would
make the name mean two things. The error names the one-line recovery. (The
implicit rungs, by contrast, fall through to auto-create, section 3.5.)

**`end` and queued messages.** Ending is the owner's statement that this
session is finished, so its unread inbox must not be silently destroyed and
must not be left where nothing drains it. `end` drains the session's inbox and
re-files each message into the owner's user mailbox (the same mailbox a bare
`user` write targets, drained by any of that user's sessions). Only then does it
release the tty name, delete the relay row, and set `ended`. A message is
therefore always in exactly one place: a live holder's inbox, or the user
mailbox. Retention expiry of a dormant session performs the same drain before
removal.

**Resume after an end.** A resume of an *ended* CLI session is a **new session**
that may reuse the name (`new --tty <name>`), not a revival of the old record.
Explicit end is intent; reviving it would contradict the verb. This differs on
purpose from the harness lifecycle, where SessionEnd is a side effect of a
process exiting and get-or-create revives the ended record. A *dormant* session
is revived, which is the CLI's version of resume.

**Interaction with the engine-session lifecycle.** A CLI session is a separate
class of record. It shares the on-disk directory convention, the `[A-Za-z0-9_-]`
id check, the `0600`/`0700` modes, and the live/dormant/ended vocabulary with
the harness lifecycle, but it never goes through harness open or SessionEnd, and
no harness hook reads or writes a `cli-` directory. The only crossing is rung 3,
where the CLI attaches to a *harness* session as a client and creates nothing.
`derive_routing_id` is unchanged; only the role argument is new.

### 3.9 Plan gate

The plan gate is keyed by the harness `session_id` in the hook payload. A plan
set through `biff plan` unlocks the gate only for the session the marker is
keyed on.

- When `CLAUDE_CODE_SESSION_ID` is present, `biff plan` keys its marker on it,
  whichever rung resolved the attachment (an explicit `-s`, the pointer and
  auto-create all attach a CLI record even inside Claude Code). The edit-gate
  hook reads that id, so the plan unlocks that session's edit gate.
- When it is absent, `biff plan` keys the marker on the CLI session's id
  (rungs 1, 2, 4, 5). No Claude session's gate ever reads it. This replaces
  today's fall-through to the shared marker bucket for a bare CLI invocation:
  the CLI always has an identity now, so the shared bucket remains only for an
  identity-less hook payload.

## 4. Stable addressing

### 4.1 The rule

The resolution rule when a `user:tty` target names no holder:

1. If some visible session holds that tty (live or dormant), deliver to it by
   its routing id. Unchanged.
2. Otherwise, if the user is known (any session of theirs is visible, or the
   user has a mailbox with a session on record), deliver to **that user's
   mailbox**, and report it to the sender: `tty3 has no active session;
   delivered to kai's mailbox (any of their sessions will see it)`. The send
   status is `forwarded`, distinct from `sent`.
3. Otherwise error: `user kai not found in visible repos`. A message is never
   delivered to a user the sender did not name.

The user mailbox already exists: a bare `user` write targets it, and `read`
drains it alongside the session inbox. The rule adds no new store, no new
address syntax, and no per-tty successor state.

### 4.2 Why this and not the alternatives

- **`@user/repo`.** Rejected. Repo is a visibility coordinate; making it part of
  the address makes it a routing coordinate, which the principle in section 2
  forbids. It also does not solve the problem: a user may have several sessions
  in one repo, and a restart still changes which one.
- **A forwarding pointer from a dead tty to its owner's current session.**
  Rejected. It needs new durable state (dead tty to successor) that must itself
  be expired, and "the owner's current session" is ambiguous when there are
  several; the pointer would have to pick one. It also routes to a *specific*
  session the sender did not name.
- **Resolve `@user` to the current session.** Close to the chosen rule, but it
  needs a "current session" concept the system does not have for a user with
  several sessions. The mailbox gives the same outcome for the common case (one
  live session reads it next) and a defined outcome for the several-session
  case (whichever of that user's sessions fetches first), without choosing.

### 4.3 Disambiguation with several sessions

The mailbox is consumed by whichever of the user's sessions reads it first, so
the message is delivered once and to a session of the right user. The sender is
told it was forwarded, and the notice lists how many live sessions the user has
and their names, so the sender can re-send to a specific one if the target
mattered. An alias with a live holder always wins over the mailbox, so a message
to a live `tty5` never leaks into the mailbox.

### 4.4 Invariants of the rule

- A send addressed to a tty is delivered to the session that holds that tty at
  send time, or to the addressed user's mailbox when nobody holds it; never to
  a session that does not hold it, and never to a different user.
- No message is dropped by a restart, an `end`, or a retention lapse: it is in
  a holder's inbox or in the user mailbox.

## 5. The Z specification change

Add a new `docs/cli-session.tex`, modeled on `docs/claude-code-biff.tex`, rather
than extending `docs/session-model.tex`.

Reasons. `session-model.tex` is the structural model of orgs, repos, machines,
clones, and engine sessions, with a routing layer and a `ROUTEMODE` scoping
parameter; the CLI session is a separate state machine (live, dormant, ended,
with a pointer and explicit transitions) whose state does not overlap any of
those components. Putting it in `session-model.tex` would bolt a second
lifecycle onto a 1500-line file whose own `Invariant` schema is already the
ProB entry point. A standalone file keeps both small enough to model-check.
`session-model.tex` stays the authority for the alias injection among engine
sessions; `cli-session.tex` restates the same injection for CLI sessions over
its own `ALIAS` set and notes the correspondence in its introduction.

**Types.** `SID`, `ALIAS`, `USER`, `REPO`, `TIME` (a bounded natural), a free
type `STATE ::= live | dormant | ended`, and a record schema `CliRecord` with
`tty`, `user`, `repo`, `created`.

**State (`CliWorld`).**

- `records : SID \pfun CliRecord`
- `state : SID \pfun STATE`, with `\dom state = \dom records`
- `usedids : \power SID`, a grow-only set, with `\dom records \subseteq usedids`
- `current : REPO \pfun SID`, the host pointer
- `inbox : SID \pfun \nat` and `usermail : USER \pfun \nat`, message counts
- `lostF`, `wrongF : ZBOOL` latches, as in `session-model.tex`

**Operations.** `New` (fresh id outside `usedids`, tty not held by a non-ended
record, writes the pointer), `Attach` (`live` stays, `dormant` becomes `live`,
`ended` and absent are not enabled), `Lapse` (`live` to `dormant`), `Use`
(enabled only for a non-ended record; sets `current`), `End` (drains `inbox`
into `usermail`, releases the alias, sets `ended`), `Expire` (drains, removes
the record), `Select` (the implicit rungs: stale pointer falls through to
`New`), and the delivery trio: `SendToHolder`, `SendStaleToMailbox`, and the
defect operation `SendMisroute` that only latches `wrongF`, enabled under a
`routeMode` scoping parameter exactly as `session-model.tex` scopes
`DeliverAliasMis`.

**Invariants to prove (state invariants, so ProB decides them over the
reachable space):**

1. **Alias injectivity.** `tty` is injective over the non-ended records:
   `\forall a, b : \dom state | state~a \neq ended \land state~b \neq ended
   \land (records~a).tty = (records~b).tty @ a = b`.
2. **Id freshness.** Every `session_id` ever minted is in `usedids`; `New`
   never reuses one, so a recycled name never inherits an inbox.
3. **Frozen identity.** `user`, `repo`, `tty`, and `created` of a record never
   change after `New` (a transition property, checked as a two-state predicate
   on every operation).
4. **Pointer validity.** Every `current` entry names a record in `records`
   whose repo equals the pointer's key. (An ended target is allowed in the
   pointer, because `Select` handles it, but `Use` never writes one.)
5. **Ended is drained.** `state~s = ended \implies inbox~s = 0`, so ending never
   strands a message.
6. **Message conservation.** The total of `inbox` plus `usermail` is changed only
   by sends and reads, never by `End`, `Expire`, `Lapse`, or `Attach`.
7. **No wrong-session delivery.** `lostF = zfalse` and `wrongF = zfalse` under the
   rule of section 4: a stale address never routes to a session that does not
   hold it. The `SendMisroute` counterexample is reachable only when
   `routeMode` selects the defect, as a fidelity control.
8. **No auto-revive of an ended session.** `Attach` and `Use` are not enabled for
   `ended`; the only way a name returns is a `New` with a fresh id.

Follow-up for the formal-methods worker: type-check with `fuzz`, model-check
with ProB, run `partition` and `audit` against the test directory, and show zero
gaps before the state-machine code lands.

## 6. Implementation write-set and commit order

### 6.1 Files

- `src/biff/commands/write.py`: the stale-target rule (section 4).
- `src/biff/server/tools/messaging.py`: the MCP `write` tool uses the same
  resolution, so the rule lives once. The resolution moves into one function
  (next to `resolve_tty_name`) returning a resolved target with a
  `forwarded` flag; both callers use it.
- `src/biff/cli_session_store.py` (new): the record dataclass (frozen) and the
  store (read, create, list, transition, expire), filesystem only, no relay.
- `src/biff/cli_session.py`: `cli_session()` takes a resolved session, stops
  calling `generate_tty()`, attaches rather than registers-and-deletes,
  and keeps the heartbeat for interactive mode.
- `src/biff/session_routing.py`, `src/biff/session_id.py`: role parameter and
  the CLI role constant.
- `src/biff/__main__.py`: the `-s/--session` global option, `BIFF_SESSION`, and
  the `session` command group (`new`, `ls`, `use`, `end`).
- `src/biff/commands/plan.py`: the marker key comes from the attached session
  (generalizing `cli_session_id()`).
- `src/biff/commands/read.py`: unchanged in behavior; it already drains the
  user mailbox, which `end` and the stale-send rule rely on. A regression test
  pins that.
- `docs/cli-session.tex`: the specification (section 5).
- Tests: `tests/test_commands/test_write.py`, a new
  `tests/test_cli_session_store.py`, `tests/test_cli_session.py`,
  `tests/test_commands/test_plan.py`, `tests/test_session_routing.py`,
  `tests/test_main.py`.
- Documentation in the same changes: `CHANGELOG.md`, `README.md`, and a new
  DESIGN.md entry written by the leader.

### 6.2 Commits

Independent units run at once, each in its own worktree and branch. The first
commit of each carries its test, written first and failing on the unchanged code.

1. **Stale-tty write forwards to the mailbox** (smallest change a user
   notices; needs none of the session work). Failing test: a `write` to
   `user:tty` with no holder returns an error today; the test asserts status
   `forwarded` and a message in the user mailbox. Also covers the unknown-user
   error and the live-holder-wins case. Touches `write.py`, the shared
   resolver, and `messaging.py`.
2. **Specification** (`docs/cli-session.tex`), in parallel with commit 1. Must
   be `fuzz`-clean and ProB-clean before commits 4 to 6.
3. **Session record store** (`cli_session_store.py` and its tests): create,
   load, list, transition, expire, modes, id validation. Independent of the
   relay; the unit tests need no mocks.
4. **Session-aware `cli_session()`** (`cli_session.py`, `session_routing.py`,
   `session_id.py`): attach without register-and-delete; a regression test that
   two invocations from one shell report the same tty. Depends on 3.
5. **`-s`, `BIFF_SESSION`, and `biff session new|ls|use|end`**
   (`__main__.py`), including the drain-on-end behavior and the
   ended-session error. Depends on 3 and 4 and the specification.
6. **Plan gate keyed by the attached session** (`plan.py`). Depends on 4.

Commits 1, 2, and 3 start immediately and in parallel. Commits 4 to 6 follow
and are sequential on the one state machine.

## 7. Open decisions for the operator

**Open decision 1: is the tty name the session id?** *Recommendation: no. The
record has an opaque `session_id`; the tty name is its alias; every verb accepts
the name.* Tradeoff. Using the name as the id removes a column and a concept.
But names are lowest-free and recycled, so a reused name would derive the same
routing id and inherit a dead session's inbox, which is the defect DES-050
removed. The opaque id costs one extra field that people never type. If the
operator still wants the name to be the id, the minimum safe version is: names
are never released for reuse until retention expiry *and* the inbox is drained
at `end`; that leaves a hole for a record deleted by hand.

**Open decision 2: inside Claude Code, does a bare `biff` command attach to the
harness session instead of creating a CLI session?** *Recommendation: yes
(rung 3).* Tradeoff. It gives one identity per Claude Code session, one
presence row, and a plan gate that a CLI `biff plan` unlocks, using the
harness-declared id the CLI already reads. The cost is that an agent cannot
hold a second, separately named CLI session from inside its own shell unless it
passes `-s` or sets `BIFF_SESSION`, which are higher rungs and so still work.
The alternative (always a CLI session, even inside Claude Code) creates a second
`ttyN` for the same working agent and leaves the plan gate unfixed.

**Open decision 3: forward or fail when a tty has no holder?** *Recommendation:
forward to the user mailbox with a visible notice (section 4.1).* Tradeoff.
Failing is the current behavior and is strictly safe, but it makes every
session restart bounce mail and forces a `who` round trip. Forwarding delivers
to the right person and tells the sender it was not the exact session. The
residual risk is that a message meant for a specific session is read by a
sibling session of the same user; the notice lets the sender correct it.

**Open decision 4: pointer scope.** *Recommendation: one pointer file per repo
under `~/.punt-labs/biff/current/`, as in section 3.5.* The operator's sketch
named a single `~/.punt-labs/biff/current` file; a single file would attach a
command in one repo to a session created for another. If a single global
pointer is preferred, the record's frozen `repo` would be checked at attach and
a mismatch would fall through to auto-create, which makes the pointer thrash
across repos.

## 8. Summary of what changes for a user

- Two commands from one shell are one session with one `ttyN`.
- `biff session new --tty main` gives a name that is stable across restarts of
  the shell; `biff -s main write ...` attaches to it from any shell.
- A send to a restarted teammate's old `user:tty` arrives in their mailbox and
  tells the sender so, instead of bouncing.
- Ending a session never loses mail; resuming a quiet session is automatic;
  resuming an ended one is a deliberate `new`.
