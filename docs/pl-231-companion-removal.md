# PL-231: Remove the dual-session companion, add a static manager label on /who

Status: DRAFT for leader review and operator ratification. No implementation
is dispatched until ratified. Mission m-2026-11-13-002 (design only).

References: PL-231, PR #455, biff-2zug, biff-qou (superseded by this design),
DES-039, DES-040, DES-043, DES-062, DES-064.

## 1. Problem

The dual-session feature registers a second "companion" session (the human)
next to the agent session inside one MCP server. It decides who the human is
by running `ethos session roster`. Ethos now requires a corroborated
`CLAUDE_PID`, and Claude Code never sets it on MCP stdio subprocesses, so the
companion can never register. DES-064 (PR #482) already deleted the roster
subprocess (`get_ethos_roster`, `ethos_session`; `tests/test_no_ethos_cli.py`
asserts they are gone). What remains is dead machinery: nothing in `src/` ever
assigns `state.companion` (`create_state(companion=...)` is only called with a
value from tests). The runtime still carries the field, the heartbeat,
reaper, teardown, inbox, unread-count and NATS-subscription branches that
consume it.

Operator ruling 2026-10-23: delete the machinery. Replace its intent with a
static "reports to MANAGER" annotation on the agent's own `/who` row,
resolved from ethos team YAML on disk. No live session lookup, no
`CLAUDE_PID`, no second TTY, KV row or NATS subscription.

Verified on current main (7597d5e): `grep -rn "state.companion\|companion="
src/` finds only reads. The production path never sets it
(`session_open.py:93` and `__main__.py:1255` call `create_state` without a
companion).

## 2. Footprint (re-grepped on main)

`git grep -il companion` returns 36 files (counted in 2.1 to 2.4). Triage:

### 2.1 Source (9 files)

| File | Verdict |
|---|---|
| `src/biff/server/state.py` | REMOVE (see 3.1) |
| `src/biff/server/app.py` | REMOVE (see 3.2) |
| `src/biff/server/tools/_descriptions.py` | REMOVE (see 3.3) |
| `src/biff/server/tools/messaging.py` | REMOVE (see 3.4) |
| `src/biff/config.py` | Docstring only: `config.py:1341` ("Companion (human) registration is deferred to the heartbeat loop") is deleted. No code. |
| `src/biff/server/tools/_session.py` | Comment only (`_session.py:37`, "the companion TTY name going unset"). Reword to "an empty tty_name". |
| `src/biff/server/activity.py` | False positive ("Companion to the frozen ServerState"). No change. |
| `src/biff/talk_state.py` | False positive (same phrase, line 79). No change. |
| `src/biff/_hook_entry.py` | False positive ("internal companion to biff.hook", line 17). No change. |

Also removed because it becomes dead: `format_read_dual` in
`src/biff/formatting.py` (`:798`, listed in `__all__` at `:45`). Its only
caller is the `state.companion` branch of `read_messages`
(`messaging.py:262-270`). `_render_message_rows` stays (still used by
`format_read`).

Brief correction: `session_id.py` has no companion code. `derive_routing_id`
takes `role` generically, and its only production caller
(`session_routing.py:42`) passes the single constant `CLAUDE_CODE_ROLE`. No
change to `session_id.py`.

### 2.2 Tests (7 files match the word; 2 more edited for dead code)

| File | Action |
|---|---|
| `tests/test_nats_e2e/test_inbox_push.py` | Delete `TestCompanionPushNotification` (`:316`) and `TestCompanionTargetedPushNotification` (`:381`); drop the `CompanionSession` import (`:24`). |
| `tests/test_server/test_active_markers.py` | Drop the companion half of the marker invariants (docstring `:7-8`, class docstring `:34`); keep the primary assertions. |
| `tests/test_server/test_app.py` | Remove `_fake_append_companion_logout` and the `"append_companion_logout"` expected call (`:241,260,328,347,363,435`); fix docstring `:281`. |
| `tests/test_server/test_descriptions.py` | Delete `test_sync_with_no_summary_still_includes_companion_count` (`:298`), `test_companion_registered_after_start_still_gets_a_sub` (`:1140`), `TestReconcileCompanionSubs` (`:1914`), `TestCompanionTalkCallbackGatesOnWakePoke` (`:2166`); fix `:2369` and `:2549` (see 3.3); drop imports `:22,28,30,38`. |
| `tests/test_server/test_lifespan_registration.py` | Delete `primary_state_with_companion` and `test_active_lifespan_does_not_register_companion_at_startup`; fix module docstring (`:4`). |
| `tests/test_server/test_state.py` | Delete `TestCompanionSession` and `TestCreateStateWithCompanion` (`:97-157`); drop import (`:12`). |
| `tests/test_session_id.py` | Reword docstring `:18` ("derives the same companion id" becomes "derives the same routing id"). The assertions stay. |
| `tests/test_server/test_formatting.py` | Delete the `format_read_dual` class (`:399-470`) and import (`:19`). |
| `tests/test_suppress_output.py` | Remove the `format_read_dual` case (`:116`) and mention (`:55`). |
| `tests/test_nats_e2e/test_push_vs_poll_latency.py`, `tests/test_relay_image/test_push_demo.py` | Check on implementation only: they print a "companion load" comparison (TESTING.md:240, CHANGELOG:175). That "companion" is the demo's idle-poll baseline, not `state.companion`. Reword only if it names the removed feature. |

The brief's 7 test files are correct for the symbol `companion`.
`test_formatting.py` and `test_suppress_output.py` are the extra two caught by
the dead-code rule.

### 2.3 Specs (formal class: must be re-proven)

| File | Companion content |
|---|---|
| `docs/session-model.tex` | `:100-103`, `:154-170`, `:1322`, `:1338`, `:1385`, plus the `human` role of `RID`/`derive`/`RegisterEntry` (see 5.1). |
| `docs/nats-relay.tex` | `SubKind` constructor `inboxNotifyCompanion` (`:228`), prose `:170-253`, `:1306-1352`, `:1628` initial `subGen`, CTL property `:1789-1842`. |
| `docs/nats-relay-kvwatch-noreconcile.tex` | Same constructor (`:228`), initial `subGen` (`:1616`), property text `:1787-1827`. The brief says "check the variants"; this one is a full copy and must change with `nats-relay.tex`. |
| `docs/sentinel-reap.tex` | Prose only: `:141-146` (companion key as a second carrier), `:650`, `:832` ("Companion sessions as a distinct carrier"). No formal clause depends on it. Re-run its four `probcli` commands (`:609,634,727,782`) after the prose edit as a check. |
| `docs/nats-relay-teardown.tex`, `docs/nats-relay-teardown-unbounded.tex` | False positives ("Companion to nats-relay.tex"). No change, but they import the `SubKind`-free teardown; re-run fuzz to prove they are independent. |

### 2.4 Docs

`git grep -il companion` returns 36 files: 9 source, 7 test, 6 `.tex` specs
(2.3) and 14 doc/other. The brief's "15 docs" counted the 14 here plus one
spec. Of the 14, two are false positives: `.claude/agents/dna.md`
(generated) and `.punt-labs/ethos/writing-styles/norman-prose.md` (unrelated
use of the word). The real 12 are `CHANGELOG.md`, `DESIGN.md`, `TESTING.md`,
`docs/coe-v1.11-release.md` and the 8 `docs/*.md` files named below.

Leader's call per direct-authoring rules; recommendation in parentheses:

- Archive under `docs/archive/` with a superseded banner (do not delete):
  `docs/dual-session.md`, `docs/dual-session-ux.md`,
  `docs/agent-first-identity.md`, `docs/spec-agent-first-identity.md`,
  `docs/design-nats-push.md`. Keep `docs/coe-dual-session-companion.md` in
  place as history (its value is the COE).
- Edit one reference each: `docs/biff_coordination_use_cases_v0.2.md`,
  `docs/session-pinned-opencode.md`, `docs/coe-v1.11-release.md:37`
  (historical table row, leave), `TESTING.md:240`.
- `CHANGELOG.md`: historical entries stay. Add `[Unreleased]` entries (7.3).
- `DESIGN.md`: see 6.

## 3. REMOVE list, symbol by symbol

### 3.1 `src/biff/server/state.py`

- `CompanionSession` dataclass (`:23`) and its `session_key` property (`:38`).
- `ServerState.companion` field (`:72`).
- `ServerState.companion_session_key` property (`:134-136`).
- `create_state(companion=...)` parameter (`:165`) and `companion=companion`
  pass-through (`:213`).

### 3.2 `src/biff/server/app.py`

Each site loses its companion branch. The primary path is unchanged.

- `:112-114` active-marker removal for the companion.
- `:280-281` `live_keys.add(companion_session_key)` in the reaper live-set.
- `:391,406-410` `_heartbeat_loop` docstring and companion heartbeat.
- `:609-610` early return when `session_key == companion_session_key`
  (reap-fallback guard).
- `:714-740` `_append_companion_logout_event` (whole function) and its call
  `:1023`.
- `:793-794` `active_keys.add(companion_session_key)`.
- `:890-905` the `companion()` cleanup step. `_run_cleanup_steps([primary,
  companion, close])` becomes `[primary, close]`.
- `:960-966` companion reap-fallback sentinel write.
- `:1162-1170` signal-cleanup companion sentinel write.
- `:1202-1218` signal-cleanup companion remove-sentinel and session delete.
- `:870` comment ("companion release") reworded.

`_poll_companion_registration` and `_register_companion` no longer exist in
`src/` (removed with the roster call in DES-064). They survive only as stale
references: `_descriptions.py:1520` comment and
`tests/test_server/test_descriptions.py:1145` docstring. Both go with the
edits in 3.3 and 2.2.

### 3.3 `src/biff/server/tools/_descriptions.py`

- Import of `CompanionSession` (`:50`).
- `_INBOX_NOTIFY_COMPANION_RESUBSCRIBE_MESSAGES` (`:95-102`) and
  `_COMPANION_TALK_RESUBSCRIBE_MESSAGES` (`:108-115`), with their comments.
- Companion half of the combined unread fetch (`:399-416`): the function
  returns the primary summary. Rename its docstring accordingly. If it then
  just wraps one `get_unread_summary` call, inline it at its callers rather
  than keep a pass-through.
- Status-file identity swap (`:462-464`): the unread status file always
  carries `state.config.user` and the primary tty name (this reverses
  biff-uw6i, which only applied when a companion existed).
- `subscribe_companion_talk` (`:984`), `_reconcile_companion_talk_sub`
  (`:1071`), `_CompanionSubs` (`:1090`), `_reconcile_companion_subs` (`:1118`).
- In `subscribe_inbox_notify` (`:1204`) keep the `user` parameter only if
  another caller passes a different user. Today the only second binding is
  the companion, so drop `user` and use `state.config.user`. The same applies
  to the helper at `:1263` and the tests at `test_descriptions.py:2369,2549`
  ("different users subject for companion", "reconciles the companion binding
  independently"): delete those two tests and fold any still-meaningful
  assertion into the own-user tests.
- In `poll_inbox` (`:1494-1530,1630-1643`): drop the `companion =
  _CompanionSubs(...)` local, the `_reconcile_companion_subs` call, and
  shrink `all_subs` from four to two entries (`talk_sub`, `inbox_notify_sub`).
- Docstring and comment sweeps at `:93,403,435,733,860,1216,1224,1519-1530`.

### 3.4 `src/biff/server/tools/messaging.py`

- `_COMP_TTY`, `_COMP_USER` labels (`:83-88`).
- `_fetch_unread_with_retry`: the `companion` local and the two extra steps
  (`:114-121`); `_UnreadFetch` becomes a 2-tuple (`:80`); the `results.get`
  slots (`:141-142`); docstring "up to four" becomes "two".
- `_mark_companion_read` (`:159-173`) and its call (`:258`).
- `read_messages` (`:236-272`): unpack two lists; the
  `format_read_dual` branch is deleted; the result is always `format_read`.

### 3.5 Companion routing-id derivation

None exists in `session_id.py` (see 2.1). Production already uses a single
role. The only code that mentions a second role is the spec (5.1).

### 3.6 `resolve_talk_target` companion-aware branches

None. `resolve_talk_target` (`_session.py:127-159`) takes `sessions`, `user`,
`tty`, `sender_key`, `sender_repo` and has no companion parameter or branch.
It is not edited. The companion only motivated DES-043 (a user may have a
companion shadow plus a REPL, so bare `@user` is ambiguous). That rationale
stays valid with a REPL and several agent sessions, so DES-043 is not
superseded (see 6).

### 3.7 Final grep gate for the implementation PR

After wave A the repo must satisfy:

`git grep -nE "CompanionSession|state\.companion|companion_session_key|_poll_companion_registration|_register_companion|inboxNotifyCompanion|_append_companion_logout_event|subscribe_companion_talk|_reconcile_companion|_CompanionSubs|format_read_dual" -- src tests docs/*.tex`

returns nothing. Archived docs are excluded from the grep by moving them
under `docs/archive/` and listing that path in a negative pathspec.

## 4. Talk session-scoping invariant (biff-sqc) stays intact

The invariant (DES-043): every talk frame carries `to_key` equal to the target
session key; a session accepts a frame only when `to_key` is empty or equal to
its own key; self-talk is refused; a bare `@user` is rejected.

It lives entirely on the primary session path and does not depend on
`state.companion`:

- Send: `TalkState._publish` (`talk_state.py:646-673`) sets `to_key` for
  invite, accept, message, end and withdraw.
- Resolve: `resolve_talk_target` (`_session.py:127`) raises on missing `tty`
  and on `key == sender_key`.
- Receive filter: `is_notification_for_session` (`tty.py:130-138`) compares
  against the receiving session's own key (`state.session_key`).

None of the three is in the 3.x removal list. The companion's own talk SUB
(`subscribe_companion_talk`) was presence-only: it never fed `TalkState`
(`_descriptions.py:1017-1024`), so removing it deletes no receive path.

Proof obligations (existing tests, which must stay green and are not edited):
`tests/test_server/test_talk.py` (`resolve_talk_target` cases at `:241-310`),
`tests/test_nats_e2e/test_talk.py:55` (bare `@user` rejected), `:179`, `:238`
(frame carries `to_key`), and `tests/test_talk_state.py` (session-scoped
receive filters). `docs/talk.tex` is not touched. The implementation PR
records the before and after pass of these files as the invariant proof.
`make fuzz SPEC=docs/talk.tex` is re-run as a negative control: it must still
pass untouched.

## 5. Spec re-proof plan

### 5.1 `docs/session-model.tex`

Decision: remove the `human` role from the routing layer.

- `RID == SESSION \cross KIND` becomes `RID == SESSION`; `derive` becomes the
  identity on `SESSION`. Production derives from one constant role, so the
  model should have one entry per session. `KIND` stays (process kind).
- `RegisterEntry`, `SuspendEntry`, `ResumeSession`, `ForkSession`, `Learn` and
  the `DeliverAlias*` schemas lose their `k` parameter. `ttyalias` becomes
  `SESSION \pinj ALIAS`.
- Prose edits: `:100-103`, `:154-170` (drop "the companion is no more
  volatile", "two roles"), `:1322` (drop "agent and companion"), `:1338-1342`
  (a session now carries one entry; the recycle scenario happens across
  suspend and re-register of that one entry), `:1385` (trace D2 rewritten
  with `agent`).

Invariants that change: the injectivity-of-`derive` invariant is now
trivial (identity function) and is deleted as a named invariant; the
`ttyalias` injection is restated over `SESSION`. The headline invariant
`routeMode = rmIdentity => lostF = zfalse /\ misF = zfalse` is unchanged in
statement and must be re-proven, not assumed.

Re-run, in order, from the repo root (`PROBCLI` defaults to
`~/Applications/ProB/probcli`):

```bash
make fuzz SPEC=docs/session-model.tex
make prob-session
probcli docs/session-model.tex -model_check -strict \
  -card NAME 1 -card HOST 1 -card PROCESS 1 \
  -card SESSION 1 -card ALIAS 2 \
  -p DEFAULT_SETSIZE 1 -p MAXINT 4 \
  -p MAX_INITIALISATIONS 100 -p MAX_OPERATIONS 20000 \
  -p TIME_OUT 60000 -coverage
```

The last command is the spec's own primary run (`session-model.tex:1345`).
Expected: `invariant_violated = 0`, no deadlock. The state counts quoted in the
spec (`9,390` states / `63,614` transitions mixed scope; `654` / `3,098`
uniform) will change because the state space shrinks. The implementation
replaces them with the new measured numbers; it does not keep stale ones.

Then the reachability goals (D2, D3, D4, spec `:1377-1423`), each run with the
same cards:

```bash
probcli docs/session-model.tex -model_check -strict -card NAME 1 -card HOST 1 \
  -card PROCESS 1 -card SESSION 1 -card ALIAS 2 -p DEFAULT_SETSIZE 1 -p MAXINT 4 \
  -goal "lostF=ztrue & routeMode=rmAlias"
probcli docs/session-model.tex -model_check -strict -card NAME 1 -card HOST 1 \
  -card PROCESS 1 -card SESSION 1 -card ALIAS 2 -p DEFAULT_SETSIZE 1 -p MAXINT 4 \
  -goal "misF=ztrue & routeMode=rmAlias"
probcli docs/session-model.tex -model_check -strict -card NAME 1 -card HOST 1 \
  -card PROCESS 1 -card SESSION 1 -card ALIAS 2 -p DEFAULT_SETSIZE 1 -p MAXINT 4 \
  -goal "(lostF=ztrue or misF=ztrue) & routeMode=rmIdentity"
```

Required outcomes: D2 and D3 FIND a trace (the defect is still reachable
under alias routing, otherwise the model lost its teeth and the proof is
vacuous); D4 reports NO such state (LOST and MISROUTED unreachable under
identity routing). D2's old trace used the `human` entry; the new trace is
`OpenHeadlessSession; RegisterEntry(s1, n2); Learn(n2); SuspendEntry(s1)`
then a route-on-alias send. D3's trace is unchanged except for the dropped
role argument. If D2 or D3 stop finding a trace, the edit over-simplified and
must be revised before merge.

Also run the z-spec zero-GAP gate: `/z-spec:audit docs/session-model.tex
--test-dir=tests`.

### 5.2 `docs/nats-relay.tex` and `docs/nats-relay-kvwatch-noreconcile.tex`

- `SubKind ::= talk | inboxNotify | inboxNotifyCompanion | kvWatch` becomes
  `SubKind ::= talk | inboxNotify | kvWatch` (`:228`).
- Initial `subGen' = { talk |-> 0, inboxNotify |-> 0, kvWatch |-> 0 }`
  (`:1628`; `:1616` in the kvwatch variant).
- Prose: `:170-186,234-253` (SubKind history), `:1306-1352` (the `Subscribe`
  explanation and the companion talk SUB paragraph, which disappears with the
  feature), `:1799-1801,1841` (the CTL property list).
- The CTL property `forall k : SubKind @ AG(connState /= closed /\
  subGen(k) < generation => EF subGen(k) = generation)` is unchanged in text.
  It is proven per instance (probcli CTL is not parametric), so the instance
  list drops from four (`talk`, `inboxNotify`, `inboxNotifyCompanion`,
  `kvWatch`) to three. The spec text says "all four instances" and "the
  three kinds" in places; every count word is corrected.
- Invariant `forall k : SubKind @ subGen(k) <= generation` (`:375`) is
  unchanged and re-checked.

Re-run:

```bash
make fuzz SPEC=docs/nats-relay.tex
make prob SPEC=docs/nats-relay.tex PROB_SETSIZE=2
make fuzz SPEC=docs/nats-relay-kvwatch-noreconcile.tex
make prob SPEC=docs/nats-relay-kvwatch-noreconcile.tex PROB_SETSIZE=2
make fuzz SPEC=docs/nats-relay-teardown.tex
make fuzz SPEC=docs/nats-relay-teardown-unbounded.tex
make fuzz SPEC=docs/sentinel-reap.tex
```

Then the per-kind CTL liveness check, three runs (one per remaining kind),
through the z-spec plugin (`/z-spec:test docs/nats-relay.tex`) or directly
with `probcli docs/nats-relay.tex -model_check -ctlformula "<formula for
k>"`, substituting `talk`, `inboxNotify`, `kvWatch` for `k`. Each must report
TRUE. The restriction to `connState = connected` is also re-run for each
kind (spec `:1802-1804`). The pre-fix negative control (`subGen(talk) = 0`
guard returning FALSE with the `generation = 3, subGen(talk) = 2`
counterexample, spec `:1830-1837`) is re-run and must still return FALSE:
that proves the liveness check still discriminates after the constructor was
removed.

I could not find the exact `-ctlformula` string stored in the repository
(`Makefile`, `biff/prob_gate.py` and the spec text do not contain it; the spec
records results, not commands). The implementer takes it from the `/z-spec:test`
invocation that produced the existing results and records the literal command
in the PR. This is a gap in the repository's reproducibility that the
implementation should close by adding the formulas as a comment block in the
spec.

Then `/z-spec:audit docs/nats-relay.tex --test-dir=tests` must show zero GAP.

`docs/sentinel-reap.tex` needs no formal change; after the prose edit its four
`probcli ... -noass -nodead` runs (`:609,634,727,782`) are re-run as given in
the spec.

## 6. DESIGN.md plan

Do not delete anything.

- DES-039 (`DESIGN.md:4363`, already "retired" by DES-064): set Status to
  `SUPERSEDED by DES-0xx (companion machinery removed, PL-231)`.
- DES-040 (`:4480`, agent-first identity): status stays SETTLED. Add a note
  that the companion half is superseded. The disk-based agent identity is
  untouched and the manager label builds on it.
- DES-043 (`:4924`, session-scoped talk): status stays SETTLED. Its decision
  does not depend on the companion; add one line to Context noting the
  companion shadow no longer exists and the ambiguity argument stands with
  REPL and multiple agents. The title "Dual-Session-Aware" is kept as the
  historical name.
- DES-062 (`:7341`, push-driven inbox): add a note that the companion
  subscription variants it describes are removed; the wake-poke design is
  otherwise unchanged.
- New DES-0xx (number assigned at write time, next free): "Remove the dual-session
  companion; static manager label on /who". Sections: Context (the
  `CLAUDE_PID` break, PR #455, biff-2zug, biff-qou superseded, operator ruling
  2026-10-23), Decision (3.x removal list, 4 invariant, 5 spec changes,
  8 manager label), Alternatives rejected (re-key the roster call on a
  declared caller: rejected, it keeps a second TTY, KV row and NATS SUBs for a
  presence-only label; ethos subprocess for the manager: rejected, ethos
  needs `CLAUDE_PID` and `tests/test_no_ethos_cli.py` forbids it),
  Consequences (status file always shows the agent; `read_messages` has one
  format), Evidence (the grep gate and the re-proof outputs).

DESIGN.md is a historical log; edits are additions plus status lines. Keep
markdownlint clean (`npx markdownlint-cli2 DESIGN.md`).

## 7. ADD: the manager label

### 7.1 Where it lives

A new module `src/biff/ethos_team.py` with one class, `EthosTeams`, which owns
the team data and the lookup. This follows PY-OO-7: `config.py` already has
many free functions around `EthosIdentity`; adding a lookup there would grow a
module that fails `method_ratio` and would put team logic next to config
parsing. A method on `EthosIdentity` was rejected because the identity is a
3-field frozen value (`handle`, `display_name`, `kind`) and does not own team
data; making it load team files would be the wrong direction (state would
depend on I/O).

```python
@final
class EthosTeams:
    """The role graph read from .punt-labs/ethos/teams/*.yaml."""

    def __new__(
        cls, roles: Mapping[str, str], reports_to: Mapping[str, str]
    ) -> Self: ...

    @classmethod
    def from_repo(cls, repo_root: Path) -> Self:
        """Read every team file under repo_root; unreadable files are skipped."""

    def manager_of(self, handle: str) -> str:
        """Return the manager role for handle; raise LookupError when none."""
```

Design points:

- Single public entry for callers: `EthosTeams.from_repo(root)` once at
  startup, then `manager_of(handle)`.
- Reads YAML with the existing `yaml.safe_load` (`pyyaml>=6.0`, already
  imported in `config.py:588`). No subprocess, no `CLAUDE_PID`, no new
  dependency. It reuses `config._load_yaml`'s failure contract (OSError and
  UnicodeDecodeError give an empty dict). Because `_load_yaml` is private to
  `config.py`, the implementer promotes it to a public `load_yaml` there
  and calls it from both places rather than copy it (no duplication).
- Lookup: handle, then role (from `members[].identity` -> `.role`), then the
  edge `collaborations[]` where `from == role` and `type == reports_to`, whose
  `to` is the manager role. Across several team files the first team that
  lists the handle wins; teams are read in sorted filename order so the
  result is deterministic (the repo has `engineering.yaml`, `gstack.yaml`,
  `website.yaml`).
- The result is the manager role name ("ceo", "coo"), not the manager's
  identity handle. Per the brief the label reads `(reports to ceo)`. Resolving
  a role to its holder is out of scope: roles can have several holders.
- Absent manager follows PY-EH-8: `manager_of` raises `LookupError` when the
  handle has no team, no role, or no `reports_to` edge. `from_repo` never
  raises: missing directory, a malformed file, a non-dict, or a malformed
  edge all produce an empty graph or are skipped.
  The single caller catches `LookupError` and renders nothing (7.3). The
  absence is the documented no-op, so the catch is not defensive coding.
- Validation at the boundary (PY-EH-1): handle and role strings are checked
  against the same identity grammar the config uses (`_resolve_identity_path`
  already enforces it) and rendered through `terminal_safe`, because team YAML
  is repo-controlled text that reaches an MCP tool response.

### 7.2 How the label reaches /who

Order of resolution at startup, in `session_open.py` (where `create_state` is
called at `:93`) and in `__main__.py:1255`:

1. `resolve_agent_identity_from_disk(repo_root)` already runs inside
   `resolve_config` (`config.py:551`, DES-040, unaffected) and yields the agent
   handle as `config.user` for the MCP server.
2. `EthosTeams.from_repo(repo_root).manager_of(config.user)` yields the role.
3. The role is stored on the session row as a new field
   `UserSession.reports_to: str = ""` (`models.py`). A pydantic field with a
   default keeps old rows valid, and an old reader ignores an unknown field
   only if the model allows it; implementation verifies `model_validate` of a
   row without the field in `tests/test_models.py`.

The label is data on the session row because `/who` renders rows fetched from
the relay for every visible session: the label must reach the viewer's
`format_who`, not just the owner's process. Computing it viewer-side would
require the viewer to have the agent's team file, which breaks cross-repo
`/who`. Writing it once at registration is cheap (one file read at startup)
and is not a second row, TTY, KV watch or NATS subscription: it is one extra
string in the row that is already written.

Render: `formatting.py`. `_format_who_kind` (`:320`) returns `"[A]"` for
agents; add a sibling `_format_who_reports_to(s)` that returns `" (reports to
ceo)"` or `""`, and append it to the NAME cell, giving
`claude:tty9 [A] (reports to ceo)` in the brief's example order. Because NAME
is a fixed `WHO_SPECS` column (`:297-305`), an unbounded value would widen
every row; the label is clipped with `_truncate(terminal_safe(...),
_MAX_LABEL_WIDTH)`, exactly as HOST is. `format_who` stays a free function in
`formatting.py` (module already a function module; no class there to own it).
No separate column is added, so the table width and the existing column
tests are unaffected for rows without a label.

Only the agent's own row carries it, because only the agent's own process
writes `reports_to` to its row. Humans and other agents show it if and only
if their own process resolved one.

Absent manager: `reports_to == ""` renders the existing row byte for byte
(no blank "(reports to )", no error, no extra row). The tests in 7.3 pin
this.

### 7.3 Tests the implementation adds (each fails before the change)

`tests/test_ethos_team.py` (new):

- manager found: `claude` (role `coo`) resolves to `ceo`; `adb` (role
  `infra-engineer`) resolves to `coo`.
- identity not in any team: `LookupError`.
- role with no `reports_to` edge (`jfreeman` / `ceo`): `LookupError`.
- edge of a different `type`: ignored.
- two team files: first sorted file wins; deterministic.
- missing `teams/` directory, empty file, non-dict YAML, `members` not a list,
  `collaborations` not a list, entries missing `from`/`to`: `from_repo`
  returns an empty graph; `manager_of` raises `LookupError`; nothing raises
  from `from_repo`.
- hostile role string (control characters, ANSI escape) is neutralised in the
  rendered label.
- real-data check: `EthosTeams.from_repo(<repo root>)` on biff's own
  `.punt-labs/ethos/teams/` resolves `adb` to `coo`.

`tests/test_server/test_formatting.py`: row with `reports_to="ceo"` shows
`(reports to ceo)` after `[A]`; row with `reports_to=""` is identical to
today's output (golden string); an over-long value is clipped.

`tests/test_server/test_who.py` (or the existing who tests): MCP `who` tool
output for a session started in a repo with team data contains the label;
started with no team data it does not and does not error.

The no-ethos-CLI guard (`tests/test_no_ethos_cli.py`) must keep passing: the
new module contains no `["ethos"` subprocess list and none of the retired
names.

CHANGELOG `[Unreleased]`: Removed (companion machinery, with the user-visible
effects: no dual `read_messages` format, status file always shows the agent);
Added (`/who` manager label). Both note PL-231.

## 8. Implementation waves

Two units, independent in write-set, each ends tested and mergeable. They can
run concurrently in separate worktrees.

### Wave A: companion removal and spec re-proof

- Python mission: worker `rmh`, evaluator `djb`
  (the removal touches reaper/sentinel/signal cleanup paths in `app.py`,
  where a missed branch could leak a KV row or sentinel file; djb reviews
  the cleanup and trust-boundary effects). Spec edits: worker `jms` (z-spec
  author), evaluator `jra`. Two missions in one PR, spec mission second
  (the spec is independent of the Python edit but the PR is one rollback
  unit).
- First commit within 30 minutes: the smallest user-visible change is
  `read_messages` and status file no longer taking companion branches
  (`messaging.py`, `state.py` field) with the tests that prove it.
- Write-set, mission 1 (`rmh`): `src/biff/server/state.py`,
  `src/biff/server/app.py`, `src/biff/server/tools/_descriptions.py`,
  `src/biff/server/tools/messaging.py`, `src/biff/server/tools/_session.py`
  (comment), `src/biff/config.py` (docstring only), `src/biff/formatting.py`
  (`format_read_dual` and `__all__`), and the tests in 2.2.
- Write-set, mission 2 (`jms`): `docs/session-model.tex`, `docs/nats-relay.tex`,
  `docs/nats-relay-kvwatch-noreconcile.tex`, `docs/sentinel-reap.tex`.
- Gate: `make check`, the 3.7 grep, the 5.1 and 5.2 commands with outputs
  pasted in the PR, zero GAP audit, and the talk tests in section 4.
- Leader-authored in the same PR (per direct-authoring rules): DESIGN.md
  (section 6), CHANGELOG, TESTING.md reference, doc archival (2.4).
- Demo: run `biff mcp` against a local relay with `/who`, `/write`,
  `/read`, and `/talk` between two sessions; observe the unread status file;
  stop with SIGTERM and SIGINT and confirm no stray sentinel or marker for a
  second session is written under `~/.punt-labs/biff/active/`.

### Wave B: manager label

- Worker `rmh`, evaluator `gvr` (API shape and typing, per biff worker table)
  with `djb` on the YAML-to-terminal path (hostile team YAML text reaches a
  tool response).
- Depends on nothing in wave A (it touches `models.py`, `formatting.py` and
  the `session_open` wiring, which wave A does not edit; `formatting.py` is
  shared with wave A only at `format_read_dual` and `format_who`, distinct
  functions). If both waves run at once, the second to merge rebases; the
  conflict is limited to `formatting.py`'s import and `__all__` block.
- Write-set: `src/biff/ethos_team.py` (new), `src/biff/config.py` (promote
  `_load_yaml` to `load_yaml`; no other change), `src/biff/models.py`
  (`UserSession.reports_to`), `src/biff/session_open.py` and
  `src/biff/__main__.py` (one call each), `src/biff/formatting.py`
  (`_format_who_reports_to` and `format_who`), the tests in 7.3, README (the
  `/who` output example), CHANGELOG.
- Demo: in this repo (which has team data), start `biff mcp` as `claude`,
  run `/who`, and expect `claude:ttyN [A] (reports to ceo)`. Repeat in a
  temp repo with no `.punt-labs/ethos/teams/` and expect today's row.
  Invalid case: a team file with malformed YAML. Boundary: a 200-character
  role name is clipped.

The manager label ships in the same release as the removal so the operator's
ruling (replace, not just delete) lands as one user-visible change.

## 9. How the design meets the acceptance criteria

- No code, test or live doc references `CompanionSession`, `state.companion`,
  `_poll_companion_registration`, `inboxNotifyCompanion`, or any dual-session
  TTY/KV/NATS machinery: the 3.7 grep gate plus archival of the dual-session
  docs.
- `/who` shows the label on an agent's own row when team data resolves one
  and nothing extra when it does not: sections 7.2 and 7.3.
- `session-model.tex` and `nats-relay.tex` pass fuzz and ProB with companion
  states and the `SubKind` constructor out of the proven invariants:
  sections 5.1 and 5.2, with D2/D3 reachability and the pre-fix negative
  control re-run so the proofs are not vacuous.
- DESIGN.md records the removal: section 6.

## 10. Open items for the leader

1. Doc footprint is 16 real docs plus 4 false positives, not 15. Confirm
   archive versus edit per 2.4.
2. The exact CTL command text is not stored in the repo (section 5.2). The
   implementer records it; confirm that is acceptable.
3. Wave A Python worker is `rmh`. Wave A spec worker is `jms` with `jra` as
   evaluator; both appear in `engineering.yaml` as roles, but the `.claude/agents/`
   roster should be checked for `jms` and `jra` before dispatch.
4. Dropping `human` from `RID` in `session-model.tex` is the one real
   modelling decision (5.1). The alternative, leaving the `human` role in the
   spec as an unused constructor, preserves a clause that models a feature
   that no longer exists. Recommendation: drop it.
