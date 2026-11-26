# biffd: tests owed by the Z specifications

These are the behavioral tests that `docs/specs/nats-relay.tex` (daemon layer)
and `docs/specs/session-model.tex` (daemon side) oblige the daemon code to
pass. They are derived by hand from each operation's guard and frame, because
`/z-spec:partition` and `/z-spec:audit` were unavailable (z-spec is not enabled
in this checkout). Every row is a GAP: the daemon code (PL-1668) did not exist
when this list was written. Each test drives the real daemon through its
client protocol, not a model of it.

## Session model

| Test | Operation | Asserts |
|------|-----------|---------|
| `test_request_before_hello_closes_and_changes_nothing` | `RequestBeforeHello` | A request, or a `hello` with a wrong token, on a fresh connection closes it; session records, runs, subscriptions and unread counts are identical before and after (invariant 2) |
| `test_first_hello_creates_record` | `HelloCreate` | An unknown session id gets one record bound to the run in `hello` |
| `test_second_client_attaches_to_existing_record` | `HelloAttach` | Same session id and run: one record, the first client's subscription undisturbed, no second record |
| `test_new_main_agent_id_is_a_new_run` | `HelloNewRun` | The record keeps its durable state, takes the new run, and the old run's subscription and pending deliveries are released |
| `test_replaced_run_call_is_refused_with_named_error` | `RequestReplacedRun` | A call from the old run gets the named error and changes nothing |
| `test_subscribe_sends_current_unread_summary` | `Subscribe` | The first frame equals the durable unread count |
| `test_each_arrival_is_delivered_once` | `Deliver` | N arrivals give N events; the summary never exceeds the durable count |
| `test_read_consumes_and_updates_every_subscriber` | `ReadUnread` | Read-once; every subscribed connection of that session sees the empty summary |
| `test_detach_keeps_session_and_unread` | `Detach` | A dropped connection leaves the record, run and unread count (invariant 3) |
| `test_session_end_removes_record` | `SessionEnd` | Only the SessionEnd hook of the current run removes the record; unread mail stays |
| `test_retention_expiry_removes_dormant_record_only` | `RetentionExpire` | A record with a connection is never expired; a dormant one past retention is |
| `test_restart_returns_records_to_dormant` | `DaemonExit`, `DaemonStart` | After restart every record exists, none has a connection |
| `test_restart_then_reattach_unread_equals_durable` | `DaemonExit`, `Subscribe` | Arrivals sent while the daemon was down are in the first summary after reattach, none lost, none counted twice (invariant 4) |
| `test_arrival_while_daemon_down_is_durable` | `Arrive` | Mail sent with no daemon running is delivered after restart |
| `test_connect_requires_running_daemon` | `Connect` | Autostart nudge, then connect |
| `test_close_session_refused_while_record_held` | `CloseSession` | A session with a daemon record cannot be closed until `SessionEnd` or expiry |

## Relay

| Test | Operation | Asserts |
|------|-----------|---------|
| `test_no_client_holds_relay_connection` | invariant `holdsHandle = {}` | No client process (`biff mcp`, `biff`, hook) opens a relay socket; `make check-layers` forbids `client` importing `engine` |
| `test_one_relay_connection_per_target` | `ClientAttach` | Any number of attached clients share one connection; attach causes no dial |
| `test_attach_does_not_dial` | `ClientAttach` | Attaching to a daemon with a disconnected relay leaves it disconnected until the first request |
| `test_detach_leaves_relay_and_subscriptions` | `ClientDetach` | After the last client detaches the connection, generation and always-on subscriptions are unchanged |
| `test_attach_refused_when_daemon_absent` | `ClientAttachRefused` | The client names the cause (not installed, or did not start in 5 s), never hangs |
| `test_attach_refused_when_relay_closed` | `ClientAttachRefused` | A shutting-down daemon answers `relayClosed` |
| `test_daemon_exit_resets_relay_state` | `DaemonExit` | A new daemon starts with no connection, generation 0, no subscription, idle watcher, empty attached set |
| `test_daemon_exit_from_wedged_relay` | `DaemonExit` | A half-open connection is escaped by killing and restarting the daemon |
| `test_daemon_start_dials_on_first_request` | `DaemonStart`, `EnsureConnected` | No dial at start; the first request dials |
| `test_absent_daemon_never_dials` | `EnsureConnected`, `EnsureConnectFailed` guards | Nothing outside a running daemon opens a relay connection |
| `test_close_only_with_no_attached_client` | `Close` guard | Relay close happens only in shutdown after the last client detaches |
| `test_attached_client_reaches_delivery` | invariant 5 | From any state with an attached client, subscriptions are rebound and arrivals are delivered (reuses the existing subscription-liveness tests, with a client attached) |

## Spec scope note

`session-model.tex` is model-checked at one session, two runs, two connections,
two aliases (87,482 states). It does not cover two sessions, so "a request
cannot act on another session's inbox" is not checked by the model; that
property needs a code test with two sessions.
