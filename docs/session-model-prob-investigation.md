# Session model ProB gate investigation

## Result

ProB 1.15.1-final reports a constraint-based deadlock for
`docs/session-model.tex`. This is **not an expected quiescent terminal
state**: the reported assignment violates the state schema. The formal
gate remains blocked pending specialist resolution of the Z translation /
constraint-checking discrepancy. No model invariant or gate was weakened.

The independent reachable-state check at uniform set size 1 completes
without a counterexample. That bounded result does not discharge the
failing constraint-based check or establish the larger mixed-scope proof.

## Reproduction and exact output

Run from the Biff repository with `probcli` on PATH:

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

The reachable-state comparison is:

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

## Gate correction

`make prob` now invokes `biff.prob_gate`. Every step uses `-strict`,
prints the complete merged stdout/stderr, and stores it under `.tmp/prob/`.
Nonzero exits, violation diagnostics, timeouts, and missing proof-completion
messages fail closed. There are no output-filtering pipelines. Failure
stops later steps and prevents both success banners.

```bash
make prob-session PROBCLI=/run/current-system/sw/bin/probcli
```

This now exits 2 at the CBC deadlock step, correctly reporting
`ProB deadlock FAILED (exit 1)`. Its exact checker output is saved in
`.tmp/prob/session-model-deadlock.log`. Resolving this formal blocker
requires deciding whether to repair ProB's schema translation / state
selection or the model's explicit state designation, not pretending that
the invalid CBC assignment is a legitimate terminal state.
