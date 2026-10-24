# Session model ProB gate investigation

## Result

The discrepancy is resolved on ProB 1.15.1-final without weakening any
invariant or removing constraint-based checking (CBC). ProZ selected
`RouteWorld` correctly through `Init`, but requires a schema named
`Invariant` to install its predicates in the translated global invariant.
Without it, that invariant contained only underlying types: relations,
not their partial-function/injection and referential-integrity predicates.
The invalid static assignment below satisfied those underlying types.

Adding `Invariant` including `RouteWorld` makes CBC pass, but exposes a
real reachable violation: `CloseSession` removed a session while preserving
its live entry, violating `dom entries` being a subset of `sessions`.
Jim approved cleanup-first verification semantics on 2026-10-24 at
11:54 UTC: automatic live presence cleanup precedes final close, with no
user-visible workflow change. `CloseSession` now includes `Delta RouteWorld`,
requires no live entries for its session, and preserves all seven routing
variables. `SuspendEntry` performs cleanup before final close; saved alias
and cached binding history remain deliberately unchanged.

The corrected bounded results are:

- Uniform size 1: CBC reports `No DEADLOCK state found`; reachable checking
  exhausts **654 states, 3,098 transitions**, with no counterexample.
- Mixed scope (NAME/HOST/PROCESS/SESSION = 1, ALIAS = 2): reachable checking
  exhausts **9,390 states, 63,614 transitions**, with zero deadlocks,
  invariant violations, unchecked invariants, or open states.
- LOST and MISROUTED goals are reachable under alias routing. Neither is
  reachable under identity routing at that exhaustive mixed scope.
- `ForkSession` and `SpawnChild` are uncovered at these bounds. No larger
  scope proof is claimed. An additional mixed-scope CBC scratch experiment
  timed out externally at 90 seconds and is not a completed proof.

Official tool semantics: [ProZ, Invariant](https://prob.hhu.de/w/index.php/ProZ).
Context7 had no documentation entry for this formal-methods ProB tool.

## Historical reproduction and exact output

Before the repair, run from the Biff repository with `probcli` on PATH:

```bash
probcli docs/session-model.tex -cbc_deadlock \
  -p DEFAULT_SETSIZE 1 -p MAXINT 4 -p TIME_OUT 60000
```

The complete captured output is `.tmp/session-cbc-deadlock.log`. Its
unfiltered result section (ANSI color sequences omitted here) is:

```text
% Starting Constraint-Based DEADLOCK check
target(all)
deadlock_counter_example_found(404,408)
*** DEADLOCK state found ***
! *** error occurred ***
! cbc_deadlock_check
*** STATE =
     ( abind={(ALIAS1|->(SESSION1|->human)),(ALIAS1|->(SESSION1|->agent))} &
       clones={(HOST1|->(NAME1|->NAME1))} &
       entries={} &
       lostF=ztrue &
       machines={HOST1} &
       members={(NAME1|->NAME1)} &
       misF=ztrue &
       orgs={NAME1} &
       pkind={(PROCESS1|->human),(PROCESS1|->agent)} &
       pname={} &
       ppar={} &
       processes={} &
       psess={(PROCESS1|->SESSION1)} &
       repos={(NAME1|->NAME1)} &
       routeMode=rmIdentity &
       savedalias={} &
       sbranch={} &
       sessions={SESSION1} &
       smach={(SESSION1|->HOST1)} &
       soper={(SESSION1|->NAME1)} &
       srepo={(SESSION1|->(NAME1|->NAME1))} &
       ttyalias={} )
*** END DEADLOCKING STATE

! Total Errors: 1, Warnings:0
```

This command exits zero without `-strict`. With `-strict`, it exits 1
and reports abnormal termination; complete output is
`.tmp/session-cbc-deadlock-strict.log`. The CBC result is a static
assignment, not an execution trace from `Init`.

The assignment violates, among others:

- `abind` and `pkind` partial-function declarations (two outputs per input).
- `dom psess = processes` and `dom pkind = processes`.
- `dom sbranch = sessions`.
- `routeMode = rmIdentity` implies both flags are `zfalse`.

The historical reachable-state comparison was:

```bash
probcli docs/session-model.tex -model_check -strict \
  -p DEFAULT_SETSIZE 1 -p MAXINT 4 -p TIME_OUT 60000 \
  -p MAX_INITIALISATIONS 100 -p MAX_OPERATIONS 5000
```

Complete output is `.tmp/session-reachable-strict.log`; result:

```text
% Model checking finished, all open states visited
Model checking time: 533 ms (569 ms walltime)
States analysed: 966
Transitions fired: 4190
No counter example found. ALL states visited.
```

That run checked only underlying global types, **not the full RouteWorld
invariant**. The historical 15,247-state mixed-scope claim had the same
limitation and is superseded, not carried forward as proof evidence.
Translation evidence and the reachable close violation are retained under
`.tmp/biff-e1ew-specialist/`: `original-pp.mch`, `explicit-pp.mch`, and
`explicit-reachable.log`.

## Gate correction

`make prob` invokes `biff.prob_gate`. Every step uses `-strict`,
prints the complete merged stdout/stderr, and stores it under `.tmp/prob/`.
Nonzero exits, violation diagnostics, timeouts, and missing proof-completion
messages fail closed. There are no output-filtering pipelines. Failure
stops later steps and prevents both success banners.

Model checking requests `-coverage` and requires a single `Coverage:` state
summary with explicit zero `deadlocked`, `invariant_violated`,
`invariant_not_checked`, and `open` counters. The completion banner alone is
not proof: missing, malformed, duplicate, or nonzero safety counters fail.
Coverage is checked only in the current subprocess output, never a stale log.
The gate counts all coverage headers and state-record starts before validating
structure, so an extra malformed record cannot hide behind a valid summary.
Safety fields must occur exactly once in the state-counter section; duplicate
or misplaced safety fields after `TOTAL_TRANSITIONS` also fail closed.

`PROB_TIMEOUT` (default `60000`) supplies ProB's `TIME_OUT` in milliseconds
for individual operations/solver calls. It is not a whole-check wall limit.
`PROB_WALL_TIMEOUT` defaults to empty, leaving each checker subprocess
unbounded; setting it passes `--wall-timeout` in seconds independently of
`--timeout`. The optional wall limit must be finite and positive. A wall
timeout preserves partial output byte-for-byte before its failure diagnostic
and stops the gate. Scope size and operation timeout must be positive;
`MAXINT` must be nonnegative. Invalid arguments fail before any checker runs.

For `session-model.tex`, a translation preflight saves the internal machine
and requires actual function, injection, domain, and delivery predicates
inside its global `INVARIANT`, before initialization or proof runs. Merely
finding those predicates inside operations does not pass. Unit regressions
cover missing translation and underlying-types-only translations.

```bash
make prob-session PROBCLI=/run/current-system/sw/bin/probcli
```

The repaired target passes with CBC retained. Complete output is saved in
`.tmp/biff-e1ew-repair/prob-session.log`; individual checker logs and the
translated machine are under `.tmp/biff-e1ew-repair/prob/`.

PR #456 follow-up validation retains complete output in
`.tmp/pr456-followup-formal/prob-session.log`, with individual checker logs
and the full-invariant translation under `.tmp/pr456-followup-formal/prob/`.
This run leaves the wall timeout unset and retains strict CBC assertions and
deadlock checks. It exhausts 654 analyzed states and 3,098 transitions;
the coverage summary reports 655 total states (including the root) with all
four safety counters zero. `DeliverAliasMis`, `ForkSession`, and `SpawnChild`
are uncovered at uniform size 1; no larger-scope proof is claimed.
Targeted gate tests also exercise explicit wall bounds,
independent timeout settings, invalid arguments, incomplete coverage, and
stale log rejection.

Independent-review parser follow-up output is retained in
`.tmp/pr456-followup-formal/prob-session-parser-followup.log`; checker logs
and translation are under `.tmp/pr456-followup-formal/parser-followup/prob/`.
The real ProB coverage record passes the strengthened parser unchanged.

Mixed-scope proof command (no invariant or deadlock suppression):

```bash
probcli docs/session-model.tex -model_check -strict \
  -card NAME 1 -card HOST 1 -card PROCESS 1 -card SESSION 1 -card ALIAS 2 \
  -p DEFAULT_SETSIZE 1 -p MAXINT 4 -p TIME_OUT 60000 \
  -p MAX_INITIALISATIONS 100 -p MAX_OPERATIONS 20000 -coverage
```

Complete mixed, goal, and cleanup-close validation logs are retained in
`.tmp/biff-e1ew-repair/`. Goal runs add `-goal` with
`lostF=ztrue & routeMode=rmAlias`, `misF=ztrue & routeMode=rmAlias`, or
`(lostF=ztrue or misF=ztrue) & routeMode=rmIdentity`; the two expected
alias-routing witnesses also use `-expcterr goal_found` with `-strict`.

`cleanup-close.log` records a CBC sequence from initialization through
registration, suspension, and successful close. `blocked-close.log` omits
suspension and reports `NO SOLUTION FOUND` (strict exit 1, expected for
this deliberately impossible sequence). `guard-regression.log` restores
the old `Delta BiffWorld` close in a scratch copy and reproduces the
dangling-entry invariant violation; the repaired exhaustive runs do not.
Merely removing the explicit guard while retaining `Delta RouteWorld`
does not reproduce the defect: the included post-state predicate already
excludes the invalid close. The guard makes that cleanup precondition
explicit rather than relying on the post-state restriction.
