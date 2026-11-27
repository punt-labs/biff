# biffd: tests owed by the Z specifications

This file records the partition and audit of `docs/specs/nats-relay.tex`
(daemon layer) and `docs/specs/session-model.tex` (daemon side), with `tests/`
as the test directory. The reports were authored by hand in the shape that
`z-spec partition` and `z-spec audit` validate and persist. The file replaces
an earlier hand-derived list.

The machine-readable reports sit beside the specifications:

| Report | Content |
|--------|---------|
| `nats-relay.partition.json`, `session-model.partition.json` | TTF partitions (accepted and rejected) for the daemon-layer operations |
| `nats-relay.audit.json`, `session-model.audit.json` | Each audited constraint, the test that covers it, or the suggested test if it has none |

The audit is scoped to the daemon layer and to the `Connection` and `Watcher`
lifecycle of `nats-relay.tex`, and to the daemon side of `session-model.tex`.
The `SessionStore` operations (`UpdateSession`, `DeleteSession`, `SetWall`,
`ClearWall`) and the structural and routing layers of `session-model.tex` are
outside it.

Provenance: the four `*.partition.json` and `*.audit.json` files were authored
by reading test files and names, and the tests were not run to confirm each
match.

## Gate status

The zero-GAP gate of CLAUDE.md applies formally once clients route through
biffd (PL-1670). Until then the GAPs below are documented, not blocking.

## Result

| Specification | Constraints audited | Covered | GAP |
|---------------|--------------------:|--------:|----:|
| `nats-relay.tex` | 32 | 20 | 12 |
| `session-model.tex` | 26 | 17 | 9 |

The daemon exists (`src/biff/daemon`, `tests/test_daemon/`), and its handshake,
attach, new-run, subscription and detach behaviour is tested. What no test
covers is everything that needs the daemon to stop and start again, the two
ways a session record ends, and every relay-side effect of attach, detach and
exit.

## GAPs: `session-model.tex`

| Constraint | Operation | Test owed |
|------------|-----------|-----------|
| `daemonUp = zfalse => opened = {}`: a restart returns every record to dormant | `DaemonSideInv` | Kill and restart the daemon; every record exists and none has a connection |
| `known \subseteq sessions` | `DaemonWorld` | The daemon holds no record for a session the structural layer lacks |
| `known'`, `runOf'`, `durable'` unchanged | `DaemonExit` | Records, runs and unread counts are identical after a restart |
| Reattach after restart: unread equals durable, none lost, none counted twice (invariant 4) | `DaemonStart`, `Subscribe` | Mail sent while the daemon was down is in the first summary after reattach |
| `Arrive` is enabled with the daemon down | `Arrive` | Mail written with no daemon running is delivered after restart |
| Every subscribed connection of the session sees the empty summary | `ReadUnread` | Two connections of one session; read on one; both see zero |
| Only the `SessionEnd` hook of the current run removes the record; unread mail stays | `SessionEnd` | Record and connections gone, unread mail kept |
| `SessionEnd` needs a connection of the current run | `SessionEnd` | A `SessionEnd` from a replaced run is refused |
| Only a record with no connection expires | `RetentionExpire` | A dormant record past retention is removed; a live one is not |

## GAPs: `nats-relay.tex`

| Constraint | Operation | Test owed |
|------------|-----------|-----------|
| `daemon = daemonAbsent => attached = {}` | `DaemonLayerInv` | Kill the daemon with a client attached; no client remains attached |
| `attached /= {} => connState /= closed` | `NatsRelayState` | Relay close is refused or clients are dropped first |
| An absent daemon holds a pristine relay: disconnected, generation 0, every `subGen(k) = 0`, idle watcher | `NatsRelayState` | A daemon started after a kill has no connection and no subscription |
| Attaching causes no dial | `ClientAttach` | Attach to a disconnected relay leaves it disconnected until the first request |
| Attach refused when the relay is closed | `ClientAttach` | Shutdown-time attach is refused |
| `relayClosed` is named | `ClientAttachRefused` | The daemon answers `relayClosed` |
| Detach leaves connection, generation and subscriptions | `ClientDetach` | State identical after the last client detaches |
| Exit is enabled in every transport state | `DaemonExit` | Wedge the relay (`halfOpen`); kill and restart the daemon; relay recovers |
| Exit resets relay state; the KV sessions survive | `DaemonExit` | Presence rows remain; all relay counters are initial |
| Start dials nothing; the first request dials | `DaemonStart` | Zero relay connections until the first request |
| Only a running daemon dials | `EnsureConnected`, `EnsureConnectFailed` | No client process opens a relay socket |
| Close needs `attached = {}` | `Close` | Relay close follows the last detach |

## Verification record

Run with `probcli -model_check -strict -p DEFAULT_SETSIZE 1 -p MAXINT 4`
(2026-11-26).

| Spec and scope | Result |
|----------------|--------|
| `session-model.tex`, gate scope (1 session, 1 run, 1 connection) | 2,342 states, all visited, 0 invariant violations; `HelloNewRun` and `RequestReplacedRun` not reached at this scope |
| `session-model.tex`, `-card RUN 2 -card CONN 2` | 15,098 states, 170,079 transitions (after `ReadUnread` and `SessionEnd` were corrected to guard on the calling connection), all visited, 0 invariant violations; every daemon operation covered |
| `nats-relay.tex`, `-card CLIENT 1`, full counters (`maxGeneration 3`, `maxEpoch 2`, `maxRestarts 2`) | 1,610,778 states, 20,314,058 transitions, all visited, 0 invariant violations, all 30 operations covered (`-mc 2000000`, about 8 minutes, 7.4 GB) |
| `nats-relay.tex`, `-card CLIENT 1`, `maxGeneration 2`, `maxEpoch 1`, `maxRestarts 1` | 177,118 states, 1,959,126 transitions, all visited, 0 invariant violations; CTL `AG(attached /= {} => EF(attached /= {} & connState = connected & every subGen(k) = generation))` TRUE |

The 87,482-state `session-model.tex` figure quoted earlier was not reproduced;
these runs use one alias (ALIAS=1). The CTL run for invariant 5 did not finish
on the full counters; the reduced-counter result is TRUE and was not checked
for vacuity.

The predicates of `Connection`, `SessionStore`, `Watcher`, `DaemonLayer` and
`DaemonSide` live in separate `...Inv` schemas: an operation written with a
delta on a schema that carries predicates gets them as guards, so ProB never
reports a break.

Negative controls (temporary mutant operations, run, then discarded; each
model run at `-card CLIENT 1` with the reduced counters):

| Mutant | ProB result |
|--------|-------------|
| `MutClientOpensHandle`: `c? in attached`, `holdsHandle' = holdsHandle \cup {c?}` | `COUNTER EXAMPLE FOUND` after 3 states: `INITIALISATION, ClientAttach(CLIENT1), MutClientOpensHandle(CLIENT1)`; `Invariant 9 is false: holdsHandle = {}` |
| `MutProvisionWhileDown`: at `disconnected`, set all three provisioned flags to `ztrue` | `COUNTER EXAMPLE FOUND` after 5 states: `INITIALISATION, EnsureConnectFailed, MutProvisionWhileDown`; `Invariant 5 is false: connState /= connected => kvProvisioned = zfalse & streamsProvisioned = zfalse & namesProvisioned = zfalse` |
| `ResetInfrastructure` with its guard `connState /= connected` removed | `COUNTER EXAMPLE FOUND` after 10 states: `INITIALISATION, EnsureConnected, ResetInfrastructure`; `Invariant 6 is false: connState = connected => kvProvisioned = ztrue & streamsProvisioned = ztrue & namesProvisioned = ztrue` |

The third row is the open finding recorded in `nats-relay.tex`, Reset
Infrastructure: the code's `reset_infrastructure` reaches that state.

## Known divergences between model and code

Each needs an owner ruling or a code change; none is closed by this PR.

| Divergence | Model | Code |
|------------|-------|------|
| A retired run reconnects | `HelloNewRun` reinstates any run id other than the current one, so `r1` can reclaim a session from `r2` | `Session.attach` refuses it, and `test_a_replaced_run_that_reconnects_is_refused_for_good` requires the refusal. Closing this needs a retired-runs component in `DaemonSide` |
| First hello dials | `ClientAttach` is `Xi Connection`: attaching causes no dial | `PresenceRow.ensure` reads the relay during the first hello |
| Fetched mail in memory | `durable` is the relay's unread count and survives `DaemonExit` | With NATS, `Methods._read` holds fetched, unacknowledged mail in process memory, outside that count, so the restart result does not cover it |
| Recovery claims | `DaemonExit` is enabled in every transport state, so the generic deadlock check cannot report a wedge and the subscription-liveness formula holds trivially through an exit | The in-process recovery claims are evidenced by the frozen daemon-free variants, not by the extended model (see `nats-relay.tex`, Daemon Exit and Start) |

## Spec scope note

`session-model.tex` is model-checked at one session. It does not cover two
sessions, so "a request cannot act on another session's inbox" is not checked by
the model; that property needs a code test with two sessions
(`tests/test_daemon/test_sessions.py` has one for the shell/agent split).
