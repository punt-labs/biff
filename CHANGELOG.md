# Changelog

## [Unreleased]

### Added

- **Agents receive talk — shared ephemeral `TalkState` + `talk_read` (biff-9la, DES-045).** The MCP server now holds one ephemeral `TalkState` — the same state machine the REPL uses — fed by an always-on NATS subscription started with the poller, so a fresh agent receives an *unsolicited* talk invite or message (previously only the REPL could). The new `talk_read` tool drains the held state and returns who wants to talk plus any queued messages. The `talk` and `read_messages` tool descriptions are marked `anthropic/alwaysLoad` so their `[TALK]` / `(N unread)` markers are never deferred behind ToolSearch — a deferred MCP tool returns a cached *base* description, so the runtime-mutated marker would otherwise be invisible to the model. The MCP server `instructions` block documents the pull/receive model.
- **`/biff:poll` unified into one arg-driven command (biff-9la).** `/biff:poll <duration>` sets the server-side poll cadence *and* schedules a recurring `/loop` model check at that interval; `/biff:poll` with no argument checks now — marker-gated, pulling `talk_read` / `read_messages` only when the tool description carries its marker. Replaces the prior interval-only setter.
- **Pending-invite withdrawal and TTL (biff-9la).** Talk gained an `ntWithdraw` frame: ending or cancelling an invite (MCP `talk_end` and the REPL invite-cancel) publishes a withdrawal that clears the invitee's `[TALK]` marker immediately, and the poller expires any pending invite after `PENDING_INVITE_TTL` (300 s) as a crash/disconnect backstop — so a stranded marker always self-heals.
- **`timestamps on|off` toggle in the REPL (biff-4uq).** A REPL-only display preference that prefixes incoming talk messages with a local `[HH:MM]` stamp. Off by default (matching prior timestamp-free talk output) and not persisted across sessions — it is a display preference, not config. Added to the REPL banner. Scoped to talk display for now; applying the toggle to `read` output is deferred pending a design decision (read timestamps live in the shared formatting layer used by the MCP tool and CLI).

### Changed

- **MCP talk now shares one implementation with the REPL (biff-9la, DES-045).** The MCP and CLI talk paths had diverged into two incompatible protocols — the REPL drained ephemeral core-NATS pub/sub into a live state machine while the MCP path read a durable JetStream inbox — which is why agent↔human talk silently dropped. Both frontends now compose the single shared `TalkState` and drain it in their own idiom (the REPL modally, the MCP server via `talk_read`), so what one side sends the other receives. The receive model and the marker→description→`tools/list_changed` lifecycle are modelled in `docs/notification.tex` and model-checked.
- **`talk` is now session-scoped under the dual-session identity model (biff-sqc, DES-043).** A user now runs several concurrent sessions (agent primary + human companion + standalone REPL), so a bare `@user` talk address is ambiguous. Talk now requires a specific session (`talk @user:ttyN`); a bare `@user` errors with a hint to run `/who` and name a session. Every talk send — invite, accept, message, and end — carries a `to_key` equal to the target session key, so the shared per-user notify subject wakes exactly one session instead of broadcasting to all of a user's sessions. Talking to your own session is refused. Cross-repo talk (`target_repo`) is preserved. In the REPL handshake, a newer invite from the same user supersedes the older pending invite, and simultaneous mutual invites resolve deterministically — the lexicographically-lower session key stays the inviter and the higher auto-accepts — so neither side blocks forever. The Z model (`docs/talk.tex`) is updated to match: session-scoped notification delivery, self-talk disallowed, invite supersession, and deterministic mutual accept. `/wall` remains the broadcast surface.
- **A half-open NATS connection now recovers in ~15s instead of the ~60-80s keepalive floor (biff-3hp).** The keepalive tuning from biff-tww (DES-041) detects a wedged connection via nats-py's PING loop in ~60-80s. A proactive detector now beats that floor: when `_tracked` records `_WEDGE_FORCE_RECONNECT_THRESHOLD` (3) consecutive runtime JS/KV timeouts on a still-connected socket — the half-open signature — the relay closes the socket and clears the cached JetStream/KV handles, so the next `_ensure_connected` dials a fresh client. Each timed-out request blocks for the nats-py JetStream request timeout (5s), so detection is bounded at ~3×5s = 15s, roughly 4-5x faster than keepalive. Three consecutive failures are required so a single slow request (which a following success clears) or a brief blip cannot force a reconnect; the counter resets on any success and the teardown fires once per wedge episode. The proactive path is skipped when nats-py's own keepalive has already declared the socket down (it owns recovery then), and is serialised on `_connect_lock` so it never races or double-tears-down a concurrent rebuild. Builds on the biff-6px consecutive-timeout counter.
- **Diagnostic logging across the NATS connection cluster (biff-6px).** A connection failure is now self-explaining in `~/.punt-labs/biff/logs/biff.log`. Connect/disconnect/reconnect lines carry connection lifetime and downtime, and a half-open wedge logs a single onset (WARNING, naming the operation, whether the socket is half-open or reconnecting, and the last-successful-request age) plus a single recovery (INFO, with wedge duration and timeout count) — replacing the previous per-tick "will retry" spam. The signal for how often and how long connections wedge is now in the log rather than requiring a source read.

### Fixed

- **Talk no longer dies silently after a NATS client replacement (biff-3hp x biff-9la).** The always-on talk subscription is bound to one `nats.connect` client. A wedge teardown (`_force_reconnect`, biff-3hp) or a give-up close drops that client and the next dial builds a fresh one with no subscription — the held handle is orphaned on the closed client. Both frontends only re-subscribed when their handle was `None`, but the orphaned handle stays non-`None`, so talk went silently dead for the rest of the session (unread/wall polling hid it by redialing every tick). At 243+ sessions the ~15s wedge detector fires routinely, so this permanently killed agent-facing talk per session — the exact channel biff-9la restored, undone by the biff-3hp force-reconnect. All three talk frontends — the MCP poller (`poll_inbox`), the REPL, and the standalone `biff talk` command (`_talk_loop`) — now track the connection generation the subscription is bound to and re-subscribe when the relay dials a new client (generation advanced), never on an in-place nats-py reconnect (same client, subscriptions replayed). The subscription-vs-generation lifecycle is modelled and model-checked as `talkSubGen` in `docs/nats-relay.tex`; a new read-only `NatsRelay.connection_generation` is the race-free discriminator. The re-subscribe is crash-safe on every frontend: a transient failure during the reconnect window is caught and retried on the next tick rather than dumping a traceback and exiting the REPL, and a *persistent* re-subscribe failure surfaces exactly once at WARNING (transient attempts stay at DEBUG) via a shared onset/recovery latch — so an operator sees "talk isn't recovering" without a per-tick flood. The REPL reconciles on its *modal* talk paths too — the connected-conversation loop (`_repl_talk`) and the invite-accept wait (`_wait_for_talk_accept`) — not only the idle tick, so a client replacement *during* a live conversation or while waiting for an accept re-binds the subscription instead of silently stranding incoming partner messages for the rest of the session.
- **The MCP `poll_inbox` talk SUB now reconciles after the tick, not before (biff-3hp x biff-9la).** The poller reconciled the always-on talk subscription at the *top* of each tick, before `_safe_tick`. But the tick's relay calls are what can trigger the wedge teardown (`_force_reconnect` / `_on_closed`) that advances `connection_generation` — so a client swap detected *this* tick was not rebound until the *next* poll interval, minutes of dead talk on the agent-facing path. The reconcile now runs after `_safe_tick`, matching the REPL idle path's post-poll ordering, so a same-tick client replacement is rebound immediately. It runs on *every* tick — including cheap no-op nap ticks (which skip only the expensive `_safe_tick` relay poll) — because a wedge teardown can advance the generation from a *background* relay call (the heartbeat loop) during a nap; the reconcile is a cheap generation compare with no relay call when nothing changed, so an unsolicited invite to an idle, napping agent is never stranded.
- **The standalone `biff talk` per-tick fetch no longer crashes the session on a client swap (biff-3hp x biff-9la).** `_talk_converse` fetched the durable inbox each tick through an unguarded `relay.fetch` / `get_nc`, which raises `NatsError`/`TimeoutError`/`OSError` when a `_force_reconnect` lands mid-fetch — the traceback exited the whole `biff talk` command, killing the session on the exact client replacement the SUB reconcile is meant to survive. The fetch is now guarded with the same latched-onset discipline as the re-subscribe: a transient error logs at DEBUG and the loop paces on to re-fetch next tick, a persistent one surfaces once at WARNING.
- **Talk is no longer permanently disabled after a NATS outage at server startup (biff-9la).** The always-on talk subscription was established once at the start of `poll_inbox`; if that first attempt failed (NATS down at startup), it was never retried, so the talk channel was silently dead for the server's lifetime even after NATS recovered. `poll_inbox` now retries `subscribe_talk` on each tick while unsubscribed, self-healing once NATS is reachable.
- **Talk invite/accept bodies are bounded at publish (biff-9la).** Only `send_message` truncated its body to `MAX_BODY_LEN`; invite and accept bodies (which carry user input) were published unbounded, so an oversized body defeated the length bound (the queue caps message *count*, not byte size). `TalkState._publish` now truncates every frame body to `MAX_BODY_LEN` centrally, and the duplicated `_MAX_BODY` constant is folded into the single `MAX_BODY_LEN`.
- **Agent↔human talk no longer silently drops (biff-9la).** A talk invite or message sent from a CLI REPL to an agent's MCP session (or vice versa) was lost: the two sides used incompatible transports (ephemeral core-NATS vs a durable JetStream inbox), so the receiver's "No new messages" was reading the wrong store. Unified on the shared `TalkState` (see Changed); verified live in both directions.
- **The `[TALK]` accept hint now names the session (biff-9la).** The marker (and `talk_read` output) rendered a bare `talk @user`, which fails with *"Talk needs a specific session"* because talk is session-scoped. The pending-invite representation now retains the inviter's tty, so the hint is a runnable `talk @user:ttyN`. `PendingInvite` validates the key is a well-formed `user:tty` at construction, so a malformed or keyless frame is dropped rather than rendered.
- **The `[TALK]` marker no longer sticks after the invite is gone (biff-9la).** The server-held pending-invite set was grow-only — draining re-added it and hangup never cleared it, with no withdrawal or expiry — so the marker stayed lit after the invite ended. A withdrawal frame (clean case) and a TTL sweep (crash/disconnect) now clear it; the marker↔activity biconditional (`talkDesc ≠ base ⟺ pending ∨ queued ∨ connected`) and its liveness are model-checked in `docs/notification.tex`.
- **Transient NATS errors no longer dump tracebacks into the interactive REPL (biff-9la).** A background heartbeat or relay hiccup printed a Python traceback (`CancelledError`, `SSL shutdown timed out`) straight to the operator's prompt. The CLI heartbeat and the `nats_relay` error-callback / connection-health transitions are demoted to INFO with `exc_info` (kept in `biff.log`), and the stderr floor is raised, so a self-healing blip stays off the terminal.
- **`/biff:poll` regained its disable path (biff-9la).** When `/biff:poll` was unified into one arg-driven command, the `n`/`off`/`stop` form that turns polling off and deletes the recurring `/biff:poll` cron was dropped — a duration started polling and a bare call checked now, but nothing could stop it. The disable branch is restored in `commands/poll.md` and `commands/poll-dev.md`: `n` (or `off`/`stop`) sets the server poll interval off and `CronDelete`s the matching recurring loop, confirming in one line.
- **A failed talk invite/accept publish no longer strands the session in a phantom state (biff-9la).** Both the REPL and MCP paths advanced the phase (`begin_invite`/`begin_connected`) — and the REPL set the session plan — *before* publishing the `send_invite`/`send_accept` frame. A transient relay failure on that publish left the session INVITING/CONNECTED with no peer (and, in the REPL, a stale `talking to …` plan), reachable only by `talk_end`. The publish is now wrapped: on failure the phase resets to idle, the REPL restores the prior plan, and both surfaces return a clear *"could not reach … — not sent"* message. The REPL mutual auto-accept publish retries once and, on persistent failure, warns that the partner may not have connected — the lower-key side connects *only* on receiving that accept (`talk.tex` `MutualAutoAccept` has no symmetric fallback), so a silently-dropped accept would strand it. Regression tests assert a `send_invite`/`send_accept` failure leaves the phase IDLE and the plan unchanged.
- **A best-effort talk withdraw/end failure no longer prints a traceback into the REPL (biff-9la).** The REPL invite-withdraw (`_withdraw_talk_invite`) and the MCP `talk_end` logged their best-effort core-NATS publish failure at `WARNING` with `exc_info`. The CLI raises the stderr handler to `WARNING`, so a transient relay hiccup still dumped a Python traceback onto the interactive prompt — the exact leak the surrounding INFO demotions were meant to close. Both are now `INFO` (kept in `biff.log`, off the terminal), consistent with the `biff.nats_relay` treatment; the local state still resets and the peer still clears via the TTL sweep.
- **The connected-talk reply hint and `/biff:poll` accept instruction now name the session (biff-9la).** The `[TALK] connected to …` marker rendered `talk @user` (bare) for the reply, and the no-arg `/biff:poll` told the model to accept with `/biff:talk @user` — both fail under session-scoped talk. Both now use the runnable `talk @user:ttyN` form that `talk_read` prints.
- **An undrained queued invite now ages out on the TTL sweep (biff-9la).** On the MCP path an invite sits in the bounded queue (lighting `[TALK]` via the queued count) until `talk_read` drains it; a never-drained invite from a crashed inviter that never withdrew was never reaped, so the marker could strand indefinitely. Queued invites are now timestamped on arrival and expired by the same `PENDING_INVITE_TTL` sweep (queued messages, which clear by draining, are untouched).
- **A `/write` mail wake-poke no longer surfaces as a phantom talk message (biff-9la).** The relay pokes the talk subject with a typeless frame (`{from, body, from_key, from_tty}`, no `type`) to wake the poller on mail arrival; `TalkNotification.from_payload` coerced a missing `type` to `message`, so `receive()` enqueued the poke and `drain_for_agent` surfaced a mail body as a talk message. `from_payload` now preserves the `type` verbatim, a frame whose type is outside the modeled set is treated as a wake poke (`is_wake_poke`) that wakes the poller *without* enqueuing, and `subscribe_talk` wakes on any non-dict payload — so a real message still enqueues, a wake poke only wakes, and nothing phantom is surfaced.
- **A pending invite is no longer lost when its target fails to resolve (biff-9la).** The MCP `talk` tool and the REPL responder consumed (popped) the pending invite *before* resolving the inviter's session; if resolution failed (inviter offline, ambiguous tty), the invite was gone and could never be accepted. Both paths now peek, resolve, and only consume the invite once resolution succeeds — a failed accept leaves the invite pending.
- **A transient failure sending a connected talk message no longer fails the whole tool call (biff-9la).** The connected-phase `send_message` publish was unguarded, so a NATS disconnect/timeout raised out of the `talk` tool. It is now wrapped like invite/accept/end: on a transient publish error it returns a *"message not sent; try again"* and leaves the connection intact (a live connection is not torn down for a transient blip).
- **The `talk` tool description recommends the right action for pending/queued activity (biff-9la).** When there were pending invites or queued messages but no active session, the dynamic description told the model to "Use `talk_end` to close" — but `talk_end` returns "No active talk session" and clears nothing. Both branches now direct `talk_read` (the action that actually reads the invite/messages).
- **`/biff:poll` no longer pulls on the bare connected marker (biff-9la).** `refresh_talk` uses the `[TALK]` prefix for connected sessions too (`[TALK] connected to …`), so the no-arg poll called `talk_read` every tick while merely connected — redundant churn. `commands/poll.md` and `commands/poll-dev.md` now pull only when the description signals new activity (`wants to talk` / `new message`), not on the connected form.
- **Starting a new talk while already in one no longer silently abandons the first (biff-9la).** When CONNECTED to peer A (or mid-invite), running `talk @B` fell through to `begin_invite` and overwrote the live session — no `end` sent to A, the local connection state discarded. The same clobber also reached the *accept* path: accepting a pending invite from B (`talk @B`, B having invited you) called `begin_connected(B)` unconditionally, overwriting the live A connection before the busy-guard on the new-invite fallthrough. Both the MCP `talk` tool and the REPL now refuse a new invite *or* a different-peer accept while busy with *"Already in a talk with {partner} — use talk_end (or 'end') first."* and send/consume nothing; sending to the connected partner, and the same-partner cases (a mutual-invite glare completing, an idempotent re-accept of the current partner), are unchanged. (This preserves the one-talk-at-a-time invariant without pre-deciding whether biff should ever support concurrent talks.)
- **`talk_listen` blocks for the next inbound frame instead of returning immediately (biff-9la).** The MCP `talk_listen` tool waited on a predicate that was true for any CONNECTED session, so during an active conversation with an empty queue it returned the idle sentinel at once rather than blocking for the partner's next message. It now waits on queued-or-pending traffic (`has_pending_traffic`), excluding the bare connected phase.
- **A partner hangup mid-batch no longer surfaces a forged frame to the agent (biff-9la).** `drain_for_agent` reset to idle *inside* its drain loop when the connected partner's `end` arrived, flipping the phase CONNECTED→IDLE for the rest of the batch — so the foreign-frame guard (which requires CONNECTED) stopped filtering, and a `message` from any sender queued after the `end` fell through and surfaced. The reset is now deferred until the whole batch drains (matching `drain_connected`), keeping the partner-key guard live so a forged frame trailing the hangup is dropped.
- **A wedged relay during a connected REPL talk no longer crashes the process (biff-9la).** The REPL connected-loop `send_message`/`send_end` were unguarded, so a `NatsError`/`TimeoutError`/`OSError` on a reconnecting relay escaped `asyncio.run`, dumped a traceback, and exited — losing the typed line. Both are now best-effort like the server twin: a failed send prints *"could not reach … — not sent; try again"* and the loop survives (a failed `end` still returns to idle).
- **A failed accept publish no longer discards the pending invite (biff-9la).** The MCP and REPL accept paths consumed the invite *before* publishing the accept; a transient publish failure lost it, so a retry sent a fresh *outbound* invite instead of re-accepting. The consumed invite is now restored on failure (`TalkState.restore_pending_invite`) so a retry re-accepts.
- **The MCP accept path no longer connects to a stale session key (biff-9la).** `talk` peeked the pending invite, then awaited target resolution; the always-on subscription or the TTL sweep could supersede or withdraw the invite during that await, after which the accept used the stale key and consumed whatever invite was now current. The invite is re-checked after the resolve await and the accept refuses with *"invite changed — try talk again"* on a mismatch.
- **The REPL now ages out stranded pending invites (biff-9la).** Only the MCP poller called `expire_stale_invites`; the REPL idle tick drained banners but never reaped, so a crashed inviter's `[TALK]` marker never cleared in the REPL until restart. The REPL tick now expires stale invites each poll, mirroring the server's active tick.
- **The agent mutual auto-accept now publishes its obligatory accept (biff-9la).** When a higher-key agent completed a simultaneous mutual invite via `drain_for_agent`, it transitioned to CONNECTED as pure local state and published nothing — but the lower-key partner connects *only* on receiving an accept (`talk.tex` `MutualAutoAccept`). `talk_read`/`talk_listen` now publish the accept the drain reports, so the partner is no longer stranded. The publish retries once and, on persistent failure, appends an agent-visible warning to the tool output (*"⚠ Couldn't confirm {partner} joined…"*) — the human path warns the operator, and the agent path must too, since an agent cannot see `biff.log`.
- **An unsolicited invite waiting in the queue no longer reads as "N new message" (biff-9la).** A fresh invite sits in the bounded queue until `talk_read` drains it into `pending_invites`; the dynamic `talk` description rendered any positive queued count as chat messages, so a new invite lit `[TALK] N new message` instead of "wants to talk." The description now inspects the queued frame types and labels a queued invite as an invite (directing `talk_read` to accept), distinct from queued chat.
- **A NATS request timeout is now charged only to the connection that owned it (`fix/tracked-timeout-race`).** `_tracked` read `self._nc` at *exception* time, so if a JS/KV request was pending when keepalive declared its client down and `_ensure_connected` dialed a fresh one, the timeout — and the resulting `_force_reconnect` — was charged to the new, live client, tearing down a healthy connection (`_force_reconnect`'s own guard captures `self._nc` too late to catch this). `_tracked` now captures the owning client *before* the await and only records the timeout / decides the force-reconnect (and, symmetrically, records success to clear the wedge latch) when `self._nc` still equals that client; a settle on a superseded client is a no-op for the live connection. The `error_cb` lifecycle callback — the only one not generation-guarded — now early-returns for a superseded generation like `disconnected_cb`/`reconnected_cb`/`closed_cb`, so a dead client's late error no longer logs as if from the live connection (the SSL-teardown suppression is retained). Regression tests assert a timeout or success on a superseded client neither reconnects nor clears the live wedge counter, that a current-client timeout still reconnects at threshold, and that a stale `error_cb` no-ops while a current one still logs.
- **Talk accept now enforces the consent boundary — only the invited session may accept (biff-sqc).** The REPL initiator's accept detector (`_check_for_accept`) flipped to "accepted" on *any* non-self `accept` frame that reached the session, ignoring who sent it. Because session-scoped talk targets a specific peer, a third party who could publish a targeted `accept` (`to_key` == the initiator's session) could make the initiator believe the addressed peer had accepted — bypassing consent. The detector now requires the accept's `from_key` to equal the invited `target_key`, which `_wait_for_talk_accept` already threads in; an accept from any other origin is ignored and the initiator keeps waiting, exactly as a mismatched `to_key` is. Regression test asserts a third-party accept does not flip the outcome.
- **A half-open NATS connection no longer wedges talk, presence, and wall inbound for ~4 minutes (biff-tww).** When the relay socket stays up but the server stops responding (a half-open connection), every JetStream/KV request raised `nats: timeout`, and the poller and heartbeat loops retried the same wedged connection indefinitely. nats-py's default keepalive (`ping_interval=120s`, `max_outstanding_pings=2`) only declares such a connection dead after ~240s, so recovery was blocked for the entire window. `nats.connect` is now tuned to `ping_interval=20s` / `max_outstanding_pings=3`, detecting a dead connection in ~60-80s and firing nats-py's own reconnect, which invalidates the cached JetStream/KV handles (DES-029) and rebuilds them on the live connection. `max_reconnect_attempts` is set to `-1` (infinite) so a prolonged outage never permanently strands an MCP server. Regression tests assert the connect keepalive stays within the detection budget and that the disconnect callback triggers handle rebuild instead of an endless retry loop.

### Security

- **A forged `end` can no longer cancel an outgoing talk invite (biff-9la, DES-043).** The MCP agent drain (`TalkState.drain_for_agent`) bound its foreign-frame guard to the connected phase, so an `end` frame arriving while *inviting* (or idle) fell through to the reset branch — a forged, targeted `end` (`to_key` == the inviter's session) would cancel the pending outbound invite, the `end`-side analog of the forged-invite-suppression that the withdrawal session-key guard closed. An `end` now drives a reset only in the connected phase, matching `talk.tex` `DrainEnd`/`DrainForeignEnd` (both guarded on `phase = tpConnected`): a non-partner `end` in any phase, and any `end` while inviting or idle, is dequeued and dropped, never a reset. Regression tests assert a forged/partner-keyed `end` while inviting leaves the invite pending and the accept still connects.
- **Talk withdrawal is session-key-guarded, and keyless control frames are dropped (biff-9la, DES-045).** The new `ntWithdraw` frame removes a pending invite only when the withdrawal's originating session-key matches the stored invite's key (`WithdrawArrive`), mirroring the accept consent boundary; a stale cross-session or foreign-keyed withdrawal is a no-op (`WithdrawForeign`). This closes two holes at once: a **non-adversarial reordering** (a user invites from session A, cancels, re-invites from session B; core-NATS gives no cross-session ordering, so a late session-A withdrawal would otherwise delete the live session-B invite — the exact stale-marker class) and a **forged targeted invite-suppression** — a forged withdrawal now requires the inviter's ephemeral session-key, the same bar as forging an accept, and only ever *removes* pending state, never connects (consent boundary intact). The receive filter also drops keyless/foreign control frames (`ReceiveNotForSession`) while preserving the keyless mail-wake poke. Modelled and model-checked in `docs/notification.tex` (the reordering bad-state proved unreachable, marker liveness preserved) with the residual documented in the `docs/talk.tex` threat model.
- **A forged frame can no longer impersonate the connected partner or hang up the call (biff-9la, DES-045).** While connected, the agent drain surfaced any targeted `message` and acted on any `end` without binding `nfrom_key` to the connected `partner_key` — so a third party who could publish to the victim's talk subject (`to_key` == the victim's session) could inject a chat line *as the partner* (every display field — `from`, `from_tty` — is wire-controlled) or force a hangup with a forged `end`, contradicting the `talk.tex` guarantee that no forged frame can "speak in another's place." Connected-phase `message`/`end` frames are now bound to the partner session key in both drain paths (`drain_connected` and `drain_for_agent`); a non-partner frame is dequeued and dropped (`DrainForeignMessage`/`DrainForeignEnd`). Modelled and model-checked in `docs/talk.tex` (invariant 11 extended to the connected phase; the forged-frame bad-states proved unreachable at a three-session-key scope).
- **The pending-invite set is bounded against a forged-invite flood (biff-9la, DES-044).** `MAX_TALK_QUEUE` bounded the inbound queue, but draining moved invites into `_pending`, a dict keyed by the wire-controlled `nfrom` — so a publisher sending invites under many distinct forged `from` strings grew `_pending` without limit (the TTL is an eventual sweep, not a cap): a memory-exhaustion DoS, the `_pending`-side analog of biff-vr4. `_pending` is now capped at `MAX_PENDING_INVITES` (100) with drop-oldest-by-arrival eviction on a new inviter (superseding an existing inviter never evicts); the eviction picks the true oldest `PendingInvite.arrived`, not the oldest-inserted key. Modelled in `docs/notification.tex` (`maxPending` bound + `TalkInviteArriveOverflow`, `#talkPending ≤ maxPending` proved across the reachable space).
- **A malformed talk frame whose `from` and session-key disagree is dropped (biff-9la).** `PendingInvite` validated that its session-key was well-formed `user:tty` but not that the key's user matched the frame's `from`. Because the accept path derives its target session from the stored session-key while the pending set is keyed by `from`, a frame with `from = A` but `from_key = B:tty` could resolve an accept addressed to `A` onto session `B` — a wrong-target connect. Construction now also rejects a `from`/key-user mismatch, so such a frame is dropped at record time and never enters `_pending`.
- **A keyless talk `message` is no longer delivered to every session (biff-9la).** The receive-side session-scope filter dropped a keyless frame only when it was a *control* frame, so a typed `message` with an empty `to_key` fell through and was enqueued for all of a user's sessions. Every modeled talk frame is session-scoped (`TalkState._publish` always sets `to_key`); `receive()` now drops any frame whose `nto` is not this session's key, matching `docs/talk.tex` `ReceiveNotForSession`. The one keyless frame that still passes — the typeless mail wake-poke — is diverted to wake-without-enqueue *before* the filter, so it is unaffected.
- **The talk notification queue is bounded at 100 with drop-oldest overflow (biff-vr4).** The REPL's inbound talk notification queue was an unbounded `asyncio.Queue`: a peer (or a flood of targeted notifications) could enqueue faster than the 2s poll drains, growing process memory without limit — a DoS vector. The single NATS enqueue site now caps the queue at `MAX_TALK_QUEUE` (100) with drop-oldest semantics, retaining the newest 100 notifications and never blocking the event loop (`put_nowait`/`get_nowait` only). The Z model `docs/talk.tex` (`ReceiveOverflow`) modelled a bounded queue the code did not enforce; it is corrected from drop-newest to state-changing drop-oldest and re-model-checked (all operations covered, all states visited, deadlock-free). Closes the talk.tex T2/T6 model-vs-code divergence found in the z-spec coverage audit.
- **Talk output sanitizes remote terminal-control sequences.** Talk message bodies and sender identifiers arrive from other users over the relay and were printed straight to the terminal. A malicious sender could embed ANSI/OSC escape sequences (cursor moves, prompt spoofing, line clears, OSC 52 clipboard writes). All talk render sites (`_drain_talk_messages`, `_drain_talk_notifications`, `_check_for_accept`) now strip non-printable characters from remote fields before display. Extended to every other render surface in biff-lbj (see below).
- **Terminal-escape sanitization extended to all render surfaces (biff-lbj).** The same class of injection existed wherever biff renders relay-sourced strings: `read` (message bodies + senders), `wall` (broadcast text + sender), `status` (wall line), `who`/`finger`/`last` (user, tty, plan, hostname, dir), the `talk` MCP tool, the REPL between-command wall banner, and wall content injected into dynamic tool descriptions and the spoken wall notification. A shared `terminal_safe` helper hoisted into `biff.formatting` is applied at every in-process render site — the output boundary — since a malicious client can bypass input-side sanitization. (The status line runs as a separate process and keeps its own equivalent escape-stripping.) Injection tests cover each site.
- **NATS connection log lines no longer emit the full relay URL (biff-6px).** A relay URL can embed `user:pass@` credentials; connection log lines now record host:port only (scheme, userinfo, path, and query stripped).

## [1.10.4] - 2026-07-04

### Fixed

- **`who` and `finger` hide dead sessions instead of showing them for up to 3 days (biff-mue).** A session's KV presence entry survives to the 3-day storage TTL, so a server that shut down, was killed, or wedged lingered in presence as `+` present (e.g. `idle 5h`) until the entry expired — because deregister depends on a signal-handler sentinel that a live peer must reap, which doesn't happen on SIGKILL or last-server shutdown. Presence liveness is now decoupled from storage retention: a shared `live_sessions` filter (new `UserSession.is_live` method + `PRESENCE_LIVENESS_SECONDS`, 2× the 60s heartbeat) is applied to **all** presence surfaces — the `who` and `finger` MCP tools *and* their CLI commands. The orphan-login detector reuses the same constant (previously an inline `120.0`).

## [1.10.3] - 2026-07-03

### Fixed

- **NATS disconnect no longer wedges every MCP server (biff-wr3).** When the relay connection dropped (`nats: unexpected EOF`), `_open_connection` re-provisioned JetStream/KV on the still-open-but-disconnected connection with no timeout, blocking forever while holding `_connect_lock` — so every heartbeat, poller tick, and tool call across all running MCP servers hung and presence froze. Provisioning is now bounded by a timeout (`_CONNECT_PROVISION_TIMEOUT`); on timeout the connection is torn down and handles reset so the next call reconnects fresh. Added a `closed_cb` that drops a permanently-closed client, and NATS errors now log their `repr` (message-less errors previously logged as an empty string, hiding the cause). Regression tests assert a blocked provision times out, releases the lock, and recovers on the next call.
- **REPL prompt no longer collides with command output (biff-1xt5).** The REPL printed output without flushing, then opened the prompt gate — letting the stdin thread's `input()` prompt (which flushes immediately) overtake the still-buffered output and land on the same line. Every gate release now routes through a `_release_prompt` helper that flushes stdout first, fixing the original command path plus the sibling talk-mode banners ("Connected to…", "Talk … ended.", "not online", handshake status) that shared the same defect. Regression tests assert the flush precedes the gate release.
- **`lock-clean` now re-resolves `punt-lux` from PyPI (biff-4uxk).** The Makefile target ran a bare `uv lock`, which reuses cached resolution and does not re-fetch `punt-lux` once the local `uv.toml` override is hidden — so a release could relock against a stale `punt-lux`. Added `--upgrade-package punt-lux` to both `uv lock` calls in the target. Hit during the v1.6.2 release.
- **`make check` no longer lints quarry transcript captures.** `.punt-labs/quarry/captures/` (machine-generated session transcripts) is now excluded from `markdownlint-cli2` and gitignored, so a local `make check` no longer fails on auto-captured scratch files. CI was unaffected — the directory does not exist on runners.

### Security

- **Least-privilege `GITHUB_TOKEN` in CI workflows.** Added a top-level `permissions: contents: read` block to `docs.yml`, `test.yml`, `lint.yml`, and `hosted-nats.yml`. These workflows only check out the repo and run linters/tests, so the token no longer defaults to the repository's broad write scope. Clears four CodeQL `actions/missing-workflow-permissions` code-scanning alerts (medium severity).

## [1.10.2] - 2026-05-29

### Fixed

- **Statusline fork bomb after data-dir migration (biff-ayc).** When `BIFF_DATA_DIR` moved from `~/.biff/` to `~/.punt-labs/biff/`, reinstalling the statusline stashed the current `biff statusline` command as the "original" — creating an infinite self-referential loop where each `biff statusline` invocation spawned another. Added a self-reference guard in `install()` (stashes `null` when the current statusLine is already biff) and a defense-in-depth check in `_resolve_original_command()` (refuses to return biff as the original command).

## [1.10.1] - 2026-05-10

### Changed

- **Agent-first identity resolution (biff-8fg3, DES-040).** The MCP server now resolves its primary identity from `.punt-labs/ethos.yaml` and `.punt-labs/ethos/identities/{agent}.yaml` at startup -- no subprocess, no race with the SessionStart hook on `claude --resume`. `load_config` is split into `load_mcp_config` (agent-first chain: disk → GitHub → OS) and `load_cli_config` (human chain: GitHub → OS). The biff CLI now consistently identifies as the human at the terminal. The companion (human) session is registered on the first heartbeat tick where `ethos session roster` becomes available, not at startup. The `get_ethos_roster()` subprocess runs on a worker thread via `asyncio.to_thread` so the heartbeat never stalls the event loop.

### Removed

- **`get_ethos_identity()` and `ResolvedConfig.root_identity`.** Both had no remaining caller after the agent-first split; per the project's no-shims rule they are deleted, not aliased.

## [1.10.0] - 2026-04-16

## [1.9.6] - 2026-04-16

### Added

- **Status bar shows human identity in dual-session (biff-uw6i)** -- when a companion session is active, the unread status file writes the human (root) identity and TTY name instead of the agent (primary). The human at the terminal sees their own address in the status bar.
- **`/read` groups messages by identity (biff-uw6i)** -- in dual-session mode, `read_messages` partitions messages into per-identity sections with `▶` headers. Human section listed first. Single-session output is unchanged.

## [1.9.5] - 2026-04-15

## [1.9.4] - 2026-04-15

### Fixed

- **Identity race on resumed sessions (biff-v4he)** — on `claude --resume`, the MCP server starts before the SessionStart hook runs `ethos iam`, so `config.user` resolves to the human identity instead of the agent. The late companion registration compared `roster.root.handle` against `config.user` and returned early when they matched (both were the human). Now compares `roster.root.handle` against `roster.primary.handle` to detect two distinct identities, then creates the companion for whichever identity `config.user` does not cover.
- **Frozen org_repos after startup (biff-v4he)** — `state.org_repos` was computed once at startup and never refreshed. Cross-repo sessions appearing after startup were invisible to `/who`. Now the heartbeat loop (60s interval) re-discovers org repos and updates `state.org_repos` when changes are detected.

## [1.9.3] - 2026-04-15

## [1.9.2] - 2026-04-15

### Fixed

- **Companion session missing on resumed sessions (biff-2mhb)** — when Claude Code resumes a session (`--resume`), the MCP server starts before the SessionStart hook populates the ethos roster. The companion was never registered. Now the first heartbeat tick re-reads the ethos roster and registers the companion if it was missed at startup.
- **Hosted NATS test assertions drift (biff-l9cl)** — two `test_hosted_e2e.py` tests asserted plan message text in `/who` output, but `/who` renders plan as a boolean `P` column. Updated assertions to check the `P` indicator and verify plan text via `/finger`.
- **Doctor NATS check fails on cold DNS cache (biff-dal2)** — `biff doctor` and `biff install` verification produced a scary traceback on first run when DNS resolver cache was cold. Now retries once after 500ms with suppressed nats.py ERROR log on the first attempt.

## [1.9.1] - 2026-04-15

## [1.9.0] - 2026-04-15

### Fixed

- **Dual-session registration drops `tty_name` (biff-dzqc)** — v1.8.0 introduced dual-session support via a two-write pattern (write row → claim TTY name → write row again with `tty_name` set). If anything failed between the two writes, the KV row was left with `tty_name=""` and `/who` rendered the raw hex TTY instead of `ttyN`. Observed in production as a companion row with `display_name="Claude Agento"` and no TTY name. The primary and companion registration paths now claim-then-write: the TTY name is reserved first, then the KV row is written exactly once with `tty_name` already populated. Extracted shared `register_session()` helper so both paths use identical logic. A claim failure now leaves no KV row behind (atomic under failure). Active-marker write failures (`~/.punt-labs/biff/active/`) no longer silently swallow `OSError` — they surface at WARNING level so missing markers can be diagnosed.

## [1.8.1] - 2026-04-14

## [1.8.0] - 2026-04-14

## [1.7.0] - 2026-04-14

### Added

- **Ethos identity resolution** — biff resolves identity from `ethos whoami --json` when available (handle, display name, kind). Falls back silently to `gh api user` then OS username. Net startup improvement: ~10ms local binary vs ~200ms GitHub API.
- **Ethos team resolution** — `ethos team for-repo --json` enriches team roster when no explicit YAML roster exists. Falls back to zero-config org discovery.
- **Kind tags in presence** — `/who` shows `[A]` in the K column for agents. `/finger` shows `[kind]` in login header. Powered by the `kind` field from ethos identity.

### Changed

- **`@` prefix removed from display output** — `/who`, `/finger`, `/read`, `/last`, `/wall` output no longer prefixes usernames with `@`. The `@` was triggering Claude REPL mention behavior. `/write` input still accepts `@user` for backwards compatibility.

## [1.6.7] - 2026-04-14

## [1.6.6] - 2026-04-11

## [1.6.5] - 2026-04-08

### Fixed

- **Wall fan-out duplicate speech (vox-0e9)** — `/wall` broadcasts fanning out to N Claude Code sessions in the same repo caused the user to hear the same sentence N times as each session spawned `vox unmute` independently. `speak_fire_and_forget` now passes `--once 600` to `vox unmute`, asking voxd to deduplicate identical text within a 10-minute window. The flag is feature-detected at runtime via `vox unmute --help`: on vox versions that do not support `--once` (pre-PR #171), biff drops the flag and falls back to the pre-dedup argv so audio still plays (with the original duplication). The default wall TTL is 1 h, so the dedup window is strictly shorter — a deliberately re-posted wall still plays.

## [1.6.4] - 2026-04-01

### Fixed

- **read_messages panel count** — PostToolUse hook showed "0 new" instead of the actual message count. The grep pattern in `suppress-output.sh` used `[a-zA-Z0-9]` but `format_read()` outputs `@user:tty` — the `@` prefix wasn't matched. Added `@` to the character class.

## [1.6.3] - 2026-04-01

### Fixed

- **PR announce hook** — removed invalid `/write @human` suggestion (not a valid biff identity). Now suggests `/wall "<message>" 10m` only, with 10-minute TTL for transient team awareness. Skips `/wall` suggestion when a wall is already active.

## [1.6.2] - 2026-04-01

## [1.6.1] - 2026-03-29

## [1.6.0] - 2026-03-29

### Fixed

- **Atomic TTY name reservation** — TTY names (`tty1`, `tty2`, etc.) are now globally unique across all repos sharing a NATS relay. Replaces optimistic write-yield-verify with NATS KV `create()` for atomic reservation. Fixes identity+tty collision that caused wrong repo attribution in `/who` and safety script misidentification. (DES-035)

### Changed

- **`biff-names` KV bucket** — new NATS KV bucket for TTY name reservations, provisioned alongside `biff-sessions`. Requires NATS state reset on first upgrade.
- **`Relay` protocol** — 5 new methods: `reserve_tty_name`, `release_tty_name`, `refresh_tty_reservation`, `list_reserved_names`, `get_tty_reservation_owner`.

### Removed

- `assign_unique_tty_name()` and `verify_tty_name()` — replaced by `claim_tty_name()` with atomic reservation.

## [1.5.1] - 2026-03-26

## [1.5.0] - 2026-03-26

### Added

- **Org-based peer discovery** — `[peers].orgs` config auto-discovers repos from NATS KV subject metadata. Eliminates per-repo peer listing for organizations. 3-10x faster than explicit per-repo queries at 15-repo scale. (DES-034)
- **Lux session status dashboard** — live dashboard showing repo, context window %, cost, and biff messaging status in the lux display surface. Statusline tees raw session JSON to `~/.punt-labs/biff/session-data/` for the applet to read. (DES-032)
- **`src/biff/unread.py`** — shared `SessionUnread`, `DisplayItemView`, and reader functions extracted from `statusline.py` for reuse by the lux applet.
- **`is_lux_enabled()` in `_stdlib.py`** — stdlib-only lux config check, extracted from `hook.py`.
- **`[lux]` optional dependency** — `punt-lux>=0.14.0` for typed element construction.

## [1.4.1] - 2026-03-15

## [1.4.0] - 2026-03-15

### Added

- **Cross-repo messaging** — `/write`, `/talk`, `/who`, `/finger`, `/last` now work across repos within an organization. Sessions in peer repos (configured via `peers` in `.biff`) are visible and addressable. (DES-030)
- **FROM_TTY column in `/read`** — sender's TTY name now appears in message inbox output for session disambiguation.
- **`display_repo_name()`** — `/who` and `/last` show repos as `owner/repo` instead of the sanitized `owner__repo` NATS form.
- **`is_notification_for_session()`** — shared helper for targeted notification filtering across all 4 receiver paths.
- **`validate_tty_name()`** — TTY names restricted to `[A-Za-z0-9_-]{1,20}` allowlist, preventing terminal escape injection.
- **62 cross-repo tests** — comprehensive test coverage for cross-repo session visibility, message delivery, authorization, and formatting.

### Changed

- **PreToolUse hook** — uses `"ask"` semantics instead of `"deny"`, so agents can proceed after setting their plan rather than halting entirely.
- **Stop hook schema** — `cc_stop` and `_hook_entry._cc_stop` both emit `{"decision": "block", "reason": ...}` instead of the old `hookSpecificOutput` wrapper.

### Fixed

- **Notification filtering by target TTY** — targeted messages (`@user:tty`) now only notify the intended session, not all sessions of that user.
- **Parallel per-repo queries** — reverted org-wide KV scan back to parallel per-repo queries to avoid cross-account permission failures.
- **Wall broadcast** — skips empty repo names instead of failing on NATS subject validation.
- **CLI talk cross-repo delivery** — interactive talk correctly resolves and delivers to sessions in peer repos.

## [1.3.6] - 2026-03-13

## [1.3.5] - 2026-03-12

### Fixed

- **Stale NATS handle recovery** — `_ensure_connected()` fast path returned cached JetStream/KV handles without checking if the TCP connection was alive. After a connection drop, all MCP tools failed permanently with `ConnectionClosedError`. Fix: fast path checks `_nc.is_closed`, `disconnected_cb` proactively invalidates cached handles, `asyncio.Lock` serializes connection creation (DES-029)

## [1.3.4] - 2026-03-10

### Fixed

- **Hook startup latency** — all hook shell scripts now invoke `biff-hook`
  (lightweight entry point) instead of `biff hook` (full CLI). The `biff`
  CLI entry point imports the entire application (nats, pydantic, fastmcp,
  typer, server, commands) before running any handler code. The new
  `biff-hook` entry point imports only the hook module and stdlib helpers.
  Measured cold improvement: 4.7s → 0.4s per SessionStart on M2 MacBook Air.

### Changed

- **Lazy `__init__.py`** — `biff/__init__.py` no longer eagerly imports the
  full package. Uses PEP 562 `__getattr__` for deferred loading. Library
  consumers (`from biff import BiffConfig`) are unaffected — imports resolve
  on first access.
- **Extracted `biff._stdlib`** — pure-stdlib helpers (`find_git_root`,
  `get_repo_slug`, `sanitize_repo_name`, `expand_bead_id`, `is_enabled`,
  session lifecycle helpers) moved from heavy modules (`config`, `server/app`,
  `server/tools/plan`) to `biff._stdlib`. Hook handlers import from here to
  avoid pulling in pydantic, nats, and the server dependency tree.

## [1.3.3] - 2026-03-10

## [1.3.2] - 2026-03-09

### Fixed

- **Session resume hang** — `_read_hook_input()` called `sys.stdin.read()`
  which blocks until EOF. When Claude Code does not close the stdin pipe
  for SessionStart resume/compact hooks, this caused an infinite hang at
  "resuming session." Fixed with non-blocking `os.read()` in a `select`
  loop (50ms inter-chunk timeout) and by removing unnecessary
  `_read_hook_input()` calls from four handlers that never used the data
  (`session-start`, `session-resume`, `session-end`, `stop`).

## [1.3.0] - 2026-03-09

### Fixed

- **Hook enabled-gate bypass** — all 10 shell hooks (pre-tool gate,
  post-bash, PR announce, session init/end/resume, stop, git post-checkout,
  git post-commit, git pre-push) now check `.biff.local` with
  `enabled=true` before dispatching to Python. Previously hooks only
  checked for `.biff` existence, causing them to fire in repos where biff
  was installed but not enabled via `biff y`.
- **POSIX grep portability** — replaced GNU `\s` extension with
  `[[:space:]]` in all hook enabled-gate patterns. `\s` is not POSIX ERE
  and was treated as literal `s` on macOS/BSD grep.

## [1.2.0] - 2026-03-09

### Added

- **Lux beads board refresh** — when lux and beads are both active, biff
  nudges Claude to refresh the beads board after `bd create`, `bd update`,
  `bd close`, and `bd dep` commands. Double-gated: no nudge if either lux
  or beads is absent.
- **Lux PR dashboard** — when lux is active and a PR is created, biff
  nudges Claude to render a PR dashboard showing status, CI checks, and
  review state via `/lux:dashboard`.
- **Bead status marker cache** — `PreToolUse` gate now reads a marker file
  instead of spawning a `bd list` subprocess on every `Edit`/`Write` call.
  Marker is written on `bd update --status=in_progress` and cleared on
  `bd close` or any status transition away from `in_progress`. Marker
  persists across sessions; first check without a marker falls back to
  subprocess and caches the result.
- **Lux and beads availability detection** — `_is_lux_enabled()` reads
  `.lux/config.md` frontmatter; `_has_beads()` checks for `.beads/`
  directory. Both are file-based (<1ms).

## [1.0.1] - 2026-03-09

### Fixed

- **install.sh missing VERSION pin** — install script now declares
  `VERSION="X.Y.Z"` and uses `uv tool install punt-biff==$VERSION` per
  distribution standards. The `punt release` CLI bumps this automatically.
  Previously, the script installed whatever version PyPI returned, which
  could lag behind during CDN propagation.
- **Python 3.14 SSL error on NATS close** — suppress
  `APPLICATION_DATA_AFTER_CLOSE_NOTIFY` raised by Python 3.14's stricter
  SSL implementation during TLS teardown. Affects `biff doctor`, relay
  disconnect, and relay close paths.
- **PreToolUse hook deny reason invisible to agents** — the hook used
  `"reason"` in hook output but Claude Code requires
  `"permissionDecisionReason"`. Agents saw a generic denial with no
  actionable instructions on how to unblock (set plan + claim bead).

## [1.0.0] - 2026-03-09

### Added

- **PreToolUse workflow gate** — Edit/Write tools are denied unless a plan is
  set (`/plan`) and a bead is claimed (`bd update --status=in_progress`). The
  gate returns actionable deny messages telling the agent what to do first.
  Implements the core Z spec invariant: no file editing without plan + bead.
- **Wall loading at SessionStart** — active wall broadcasts are injected into
  session startup context so newly joining agents see team announcements.
- **Stop hook unread reminder** — soft gate injects "You have N unread messages.
  Run /read before finishing." into context when the agent stops with unread
  messages. Never blocks — just reminds.
- **PR announce wall dedup** — `handle_post_pr` checks for an active wall before
  suggesting `/wall`. When a wall is already active, only suggests `/write`.
- **`biff.markers` module** — shared marker file infrastructure for bridging
  async relay state to synchronous hooks. Plan and wall markers enable the
  PreToolUse gate and SessionStart wall injection without async relay queries.

### Changed

- **`_hint_dir` refactored** — hint directory computation extracted to
  `biff.markers.hint_dir()` shared by both hook handlers and MCP tools.
- **Plan tool side effect** — `/plan` writes/clears a `plan-active` marker file
  for the PreToolUse gate.
- **Wall tool side effect** — `/wall` writes/clears a `wall-active` marker file
  with JSON payload including expiry timestamp.

### Fixed

- **Stale plan marker on new session** — `handle_session_start` clears the
  plan-active marker to prevent a crashed session's marker from bypassing the
  PreToolUse gate.

- **CI notifications via `biff enable`** — `biff enable` deploys a standalone
  `biff-notify.yml` workflow using GitHub's `workflow_run` trigger. Fires on
  any workflow failure (push only), posts `biff wall` with a link to the broken
  run. `biff disable` removes it. `biff doctor` reports workflow status.
- **`--user` global flag** — identity override for CI bots and headless
  environments (`biff --user github-actions wall "CI failed"`)
- **`github-actions` auto-enrolled** — `biff enable` adds `github-actions` to
  the `.biff` team roster automatically so CI wall messages are accepted

## [0.17.0] - 2026-03-08

### Added

- **Interactive REPL** — `biff` with no args launches a persistent interactive
  session with full BSD command vocabulary. Unified session lifecycle (connect,
  register in KV, auto-assign ttyN, wtmp login/logout, clean shutdown) replaces
  the ephemeral 5-minute TTL session hack. All CLI modes (REPL, inline, talk)
  share one `cli_session()` context manager. (#biff-vrk)
- **REPL readline** — line editing (arrow keys, Home/End, Ctrl-A/E), command
  history persisted to `~/.punt-labs/biff/repl_history`, and tab completion for command
  names. Detects libedit (macOS) vs GNU readline for correct binding syntax.
  (#biff-x6s)
- **REPL inline notifications** — real-time NATS-driven message alerts and wall
  broadcasts while idle at the prompt. Self-notification prevention via state
  sync. Prompt gate prevents output/prompt interleave. Session prompt shows
  `user:ttyN>`. (#biff-02y)
- **REPL talk mode** — `talk @user` enters modal conversation mode (like BSD
  talk). Typed lines deliver to target, `end` returns to REPL. Incoming messages
  display as banners via NATS notification queue. Talk invitation includes tty
  for easy reply. (#biff-q07)
- **Two-phase talk handshake** — BSD-style talk protocol: inviter waits for
  accept before entering talk mode, responder detects pending invite via
  persistent set, mutual hangup ends both sides. Talk messages use NATS core
  publish (no inbox pollution). Cyan color for incoming, `user:tty >` prompt
  style. (#biff-1di)
- **Z specifications** — formal Z specs for talk handshake (`docs/talk.tex`,
  565K states) and REPL state machine (`docs/repl.tex`, 509K states), both
  verified with ProB (no counter-examples). Makefile targets for fuzz
  type-checking and ProB animation/model-checking. (#biff-1di)
- **CLI multi-user test tier** — tier 2b integration tests: two CLI users
  sharing a local NATS relay exercise who, finger, write, read, wall, plan,
  talk, and last via `cli_session`. 102 new tests total across talk partition
  tests, REPL loop control flow, and NotifyState boundary tests. (#biff-s8d,
  #biff-1di)
- **Session identity model** — Z specification (`docs/session-model.tex`)
  modeling the full organizational hierarchy from orgs to processes, with design
  documents exploring dual identity for human+agent sessions.
- **Wall broadcasts voiced via vox** — when vox is installed, wall messages are
  spoken aloud with emoticon-to-vibe mapping (`:D` becomes excited, `!!` becomes
  urgent). L0/L1 peer integration: graceful degradation when vox is absent.
  (#biff-a4d)

### Fixed

- **PostToolUse hook matches standalone MCP tool names** — the matcher
  `mcp__plugin_biff(-dev)?_tty__.*` only matched plugin-namespaced tools.
  Standalone MCP servers use `mcp__tty__*` which didn't match. Fix:
  `mcp__(plugin_biff(-dev)?_)?tty__.*` matches both patterns. (#biff-hv5)
- **TTY name race condition** — two sessions starting simultaneously could both
  compute the same ttyN name (TOCTOU between get_sessions and update_session).
  Fix: two-phase write-then-verify approach. `assign_unique_tty_name()` computes
  the name, caller writes to KV, `verify_tty_name()` re-reads and reassigns on
  collision. (#biff-1rn)
- **suppress-output.sh counts data rows not wrapped lines** — `wc -l` inflated
  row counts due to 80-char line wrapping. Each handler now uses format-specific
  grep patterns matching actual output rows. (#biff-1hn)

### Changed

- **Makefile gains Z spec targets** — `make fuzz SPEC=...` for type-checking,
  `make prob SPEC=...` for ProB animation and model-checking,
  `make prob-session` for the session identity model. `clean-tex` covers
  `docs/` artifacts.

## [0.15.1] - 2026-03-06

### Fixed

- Show help when `biff` is invoked with no command
- Fire-and-forget MCP tools no longer block on NATS publish; argv hoisting removed
- Test suite respects TMPDIR for config isolation

### Changed

- Add Makefile per makefile.md standard

## [0.15.0] - 2026-03-06

### Added

- **CLI parity with MCP tools** — every MCP product tool is now available as `biff <command>`:
  `who`, `finger`, `write`, `read`, `plan`, `last`, `wall`, `mesg`, `tty`, `status`
- **`--json` global flag** — machine-readable JSON output on all CLI commands
- **`biff status`** — connection state, active session, unread count, wall posts
- **Library API** — `from biff import commands, CliContext, CommandResult` for programmatic use;
  each CLI command is a pure async function that takes context + args and returns `CommandResult`
- **Testable command functions** — `biff.commands` module with 10 pure async functions
  (`who`, `finger`, `write`, `read`, `plan`, `last`, `wall`, `mesg`, `tty`, `status`);
  callable directly with `LocalRelay` for unit testing without NATS
- **Shared formatting module** — `biff.formatting` extracts domain-level format functions
  from MCP tool closures for reuse by both CLI and MCP surfaces
- **CLI session manager** — pseudo-ephemeral NATS sessions with 5-minute TTL for
  consecutive CLI commands (`biff.cli_session`)

### Fixed

- **Notification deferral** — NATS talk callbacks and KV wall watchers no longer fire MCP
  notifications directly (unreliable from background coroutine contexts). Both now wake the
  poller via `ActivityTracker.wake()`, which resets `_last_nap_poll` to epoch so `_active_tick`
  runs on the next 2s tick even during napping. Worst-case notification latency: ≤2s.
- **KV watcher survives snapshot-done** — `_run_kv_watch` uses explicit `updates()` loop
  instead of `async for`, surviving `None` snapshot-done markers and `TimeoutError` without
  restarting the watcher
- Suppress asyncio `eof_received` warning on NATS SSL disconnect
- Suppress nats.py deprecation warnings and disconnect noise in CLI
- Standards compliance — CLI flags, hooks, commands, suppress-output

### Changed

- CLI product commands now delegate to `biff.commands` pure async functions via a `_run()` adapter,
  replacing inline `_*_async` implementations with one-liner delegations
- `CliContext.relay` widened from `NatsRelay` to `Relay` protocol, enabling `LocalRelay` in tests
- Moved primitive formatting layer from `biff.server.tools._formatting` to `biff._formatting`
  to break circular import between `biff.formatting` and the server tools package
- Adopt dev/prod plugin namespace isolation: `plugin.json` name is `"biff-dev"` on main,
  release scripts swap to `"biff"` on tagged commits only. Dev commands (`*-dev.md`) route
  to `mcp__plugin_biff-dev_tty__*` to avoid collisions with the installed production plugin.
- Z specification amended: `KVWallReceive` and `NatsTalkCallback` defer notification to
  `PollTick`; formally verified with ProB (550K+ states, no counter-examples)

## [0.13.0] - 2026-03-02

### Fixed

- Talk push notifications from CLI now reach MCP session — `_session` is eagerly captured during `initialize` via `SessionCaptureMiddleware`, fixing the suspenders notification path for NATS callbacks (biff-8g0)

### Changed

- Status line shows dim `/biff y to enable team communication` hint when biff is not enabled for the repo, replacing the unhelpful bare `biff` label

## [0.12.2] - 2026-02-28

### Fixed

- `gh` CLI check in `biff doctor` is now optional — users without `gh auth login` no longer see a required failure or a non-zero exit code
- Installer uses `doctor || true` so diagnostic failures don't abort the install script under `set -eu`

## [0.12.1] - 2026-02-28

### Fixed

- Installer auto-installs Python 3.13 via `uv python install` when system Python is too old (Ubuntu 24.04 ships 3.12)
- Installer checks for git before marketplace operations, failing fast with a clear message instead of opaque errors
- Installer uses uninstall-before-install for idempotency (`claude plugin update` is unreliable)
- Installer adds read-after-write verification after plugin install

## [0.12.0] - 2026-02-27

### Added

- **SessionStart collision detection** — when multiple Claude Code sessions are
  active in the same worktree, the SessionStart hook emits an advisory suggesting
  `/who` and worktree usage to avoid conflicts. Active session files now include
  worktree root for precise matching.

### Fixed

- Installer now refreshes marketplace clone before plugin install, ensuring existing users get the correct `source.ref` pins

## [0.11.4] - 2026-02-26

### Fixed

- **Installer now installs from PyPI** — `install.sh` installs the released
  `punt-biff` package instead of building from git source. Faster installs,
  tested artifacts.
- **Re-running installer upgrades the plugin** — `biff install` now calls
  `claude plugin update` when the plugin is already installed, so existing
  users get new versions without manual intervention.
- **SSH-less users can install** — `install.sh` detects missing SSH keys and
  temporarily rewrites git URLs to HTTPS for `claude plugin install`.
- **Install failures show error messages** — `biff install` and `biff doctor`
  are now wrapped in `if !` guards so `set -eu` doesn't cause silent exits.

## [0.11.3] - 2026-02-26

### Fixed

- **MCP server launch no longer requires `uv`** — production `plugin.json`
  now invokes `biff serve` directly instead of `uv run biff serve`. Users
  who install via `uv tool install punt-biff` have the binary on their PATH;
  the `uv run` wrapper was an unnecessary dependency that could also pick up
  wrong project environments.

## [0.11.2] - 2026-02-26

### Fixed

- **Dev commands no longer ship to marketplace users** — moved `*-dev.md`
  commands from `commands/` (plugin-shipped) to `.claude/commands/`
  (project-local). Dev commands now only load when working in the biff repo.

## [0.11.1] - 2026-02-26

### Fixed

- **Wall messages now actually rotate** — each wall post gets a unique source
  key (`wall:{posted_at}`) so multiple walls accumulate in the display queue
  instead of replacing each other. Old walls expire naturally based on their
  original duration.
- **Statusline reads all display items** — unread file now contains a
  `display_items` array (replacing single `display_text`/`display_kind`).
  The statusline does time-based rotation (`int(time / 15) % n`) — stateless,
  deterministic, no persisted index needed.

### Added

- **`DisplayItem.expires_at`** — optional monotonic timestamp for automatic
  expiry. Expired items are purged on `current()` and `advance_if_due()`.
- **`DisplayQueue.expires_from_now()`** — helper for computing monotonic expiry
  from wall-clock remaining seconds.

## [0.11.0] - 2026-02-26

### Added

- **Display queue for status bar rotation** — wall and talk items now rotate on
  status bar line 2 (15s per turn). Wall items cycle indefinitely until they
  expire or are cleared. Talk items show once then discard. Multiple wall
  broadcasts rotate so none are hidden. (#biff-j8b)

### Changed

- **Talk messages coalesce per sender** — rapid messages from the same sender
  replace the previous queue item instead of growing the queue without bound.
- **Talk queue clears on partner switch** — changing talk partners removes stale
  messages from the previous conversation immediately.
- **Unified unread file schema** — `display_items` array replaces
  separate `wall`/`wall_from`/`talk_partner`/`talk_message` fields.
- **Injected clock for DisplayQueue** — `clock` parameter (defaults to
  `time.monotonic`) enables deterministic testing without `time.sleep`.
- **Dev/prod namespace isolation** — plugin commands and MCP server names are
  namespaced for dev/prod isolation per punt-kit plugins.md standard. (#81)
- **Repository URL in project metadata** — `pyproject.toml` now includes
  `project.urls` per punt-kit standard. (#80)

## [0.10.6] - 2026-02-25

### Fixed

- **Talk status bar updates are now instant** — the `talk` tool description is now
  mutated when talk messages arrive (e.g. `[TALK] @sender: message...`), mirroring
  the wall pattern. Previously, `notify_tool_list_changed()` fired but Claude Code
  saw no tool description change and skipped the UI re-render. (DES-020)

### Changed

- **`refresh_talk()` added** — mirrors `refresh_wall()`. Mutates the talk tool
  description, fires `notify_tool_list_changed()`, and rewrites the unread file.
- **`_sync_talk_to_file` deleted** — replaced by `refresh_talk()` which handles
  both tool description mutation and file write.
- **`_notify_tool_list_changed` → `notify_tool_list_changed`** — made public
  since it is called from `refresh_talk()` and `refresh_wall()`.

## [0.10.5] - 2026-02-25

### Fixed

- **Status bar latency regression fixed** — wall and talk updates now arrive within
  0-2s on idle sessions instead of 2+ minutes. Root cause: nap mode disconnected the
  NATS TCP connection, killing all KV watches and subscriptions. Now nap mode keeps the
  connection alive and reduces polling frequency instead. (DES-019)

### Changed

- **KV watcher detects wall changes** — `_run_kv_watch` now routes wall key updates
  through `refresh_wall()` → `_notify_tool_list_changed()` for instant push notifications
  to Claude Code. Previously wall changes were only detected by the 2s poller.
- **Heartbeat fires during nap** — idle sessions maintain heartbeat on schedule
  regardless of nap state, preventing session liveness gaps.
- **POP-mode connection cycling eliminated** — `_pop_fetch()` removed, `disconnect()`
  no longer called during nap. NATS connection persists for the full server lifetime.

## [0.10.4] - 2026-02-25

### Fixed

- **Talk push notifications are immediate** — incoming talk messages now trigger
  `_sync_talk_to_file()` and `_notify_tool_list_changed()` directly in the NATS
  callback instead of waiting for the 2s poller tick. Status bar updates appear
  within 0-2s instead of 4-6s (or never, if the poller was napping).

### Changed

- **Release process updated in CLAUDE.md** — both channels (marketplace + PyPI)
  now ship together on every version bump. Removed "milestone only" PyPI policy.
  Documented that local editable installs must never be used and that `twine upload`
  must never be run manually.

## [0.10.3] - 2026-02-25

### Fixed

- **Heartbeat no longer overwrites session metadata** — the heartbeat error path
  created a bare `UserSession(user, tty)` when KV reads failed, destroying
  `tty_name`, `plan`, `hostname`, and other fields. On hosted NATS, transient
  network issues triggered this regularly, wiping session identity. Now heartbeat
  skips missing or corrupt sessions instead of overwriting them.
- **Talk resolves tty_name before delivery** — `/talk @user:tty1` now maps
  friendly tty names to hex session keys via `resolve_session` before message
  delivery. Without this, messages addressed to tty_name failed to route.
- **Validate sender_key in deliver()** — `_validated_sender_key()` checks format
  (`user:tty`) and user-part consistency before embedding in talk notifications.
  Invalid keys are silently dropped.

### Changed

- **POP interval lowered to 10s** — idle sessions now check for messages every
  10 seconds (was 10 minutes). Idle threshold restored to 120s (was 30s).
  Status bar updates are no longer delayed by minutes during nap cycles.

## [0.10.2] - 2026-02-25

### Fixed

- **Talk self-echo on status bar** — when both sides of a `/talk` are the same
  user (different ttys), outgoing messages echoed on the sender's own status bar.
  Notification payload now includes `from_key` (sender session key) so the
  callback rejects notifications from the current session.
- **talk_listen no longer encourages loop** — updated tool description to say
  "agent-to-agent only" and "human sessions should NOT call this." The old
  description actively encouraged `talk_listen` loops, overriding the `/talk`
  command's status-bar auto-read instructions.

## [0.10.1] - 2026-02-25

### Fixed

- **Talk honors `:tty` address targeting** — `/talk @user:tty` was parsing the
  address but discarding the tty, delivering messages to the user-level inbox
  instead of the targeted session. Now `set_talk_partner` stores the full address,
  `deliver()` targets the specific tty, and the notification filter extracts the
  user-part for comparison.

## [0.10.0] - 2026-02-25

### Changed

- **Talk v2: status-line auto-read** — `/talk` now displays incoming messages on
  the status bar within 0-2s instead of blocking on `talk_listen`. The background
  poller subscribes to NATS core pub/sub notifications and writes talk messages to
  the unread status file. Line 2 priority: talk (bold yellow) > wall (bold red) >
  idle. Both parties agree to `/talk`, then auto-read each other — no `/read`
  needed. (#biff-q97)
- **NATS talk notifications carry message body** — `_publish_talk_notification`
  now sends JSON with sender and body (was a bare `b"1"` wake signal). The poller
  NATS subscription captures message content for status bar display.
- **`/talk` command updated** — no more `talk_listen` loop. Incoming messages
  appear on the status bar automatically. Use `/write` to reply, `/talk end` to
  close.

## [0.9.1] - 2026-02-25

### Fixed

- **Missing `/talk` slash command** — added `talk.md` command file so `/talk`
  appears in the skills list and deploys via SessionStart hook. The MCP tools
  existed since v0.9.0 but the slash command was never created.
- **Uninstall cleanup** — added `talk.md` to `BIFF_COMMANDS` in installer so
  `biff uninstall` removes it from `~/.claude/commands/`.

## [0.9.0] - 2026-02-25

### Added

- **Real-time talk** — three new MCP tools (`/talk`, `/talk_listen`, `/talk_end`)
  for real-time bidirectional conversation between biff sessions. Supports
  human↔agent, human↔human, and agent↔agent conversations. Uses NATS core
  pub/sub for instant notification with subscribe-before-check pattern to
  prevent race conditions. (#biff-8t3)
- **`biff talk` CLI** — `biff talk @user [message]` command for terminal-based
  interactive conversations. Single persistent stdin reader thread, NATS
  notification-driven message display, online presence check before connecting.

## [0.8.2] - 2026-02-24

### Fixed

- **Wall tty in status bar** — `_wall_from` now includes the sender's tty name
  so the status bar shows it (was only in tool description after v0.8.1).
- **Redundant session fetch** — wall tool reuses `update_current_session` return
  value instead of calling `get_or_create_session` a second time.
- **README image on PyPI** — use absolute GitHub URL for `biff.png` so it renders
  on pypi.org (relative paths don't resolve there).

## [0.8.1] - 2026-02-24

### Fixed

- **Wall sender tty** — `/wall` now includes the sender's tty name (e.g.
  `@kai (main)`) in the wall output, tool description, and status bar. Previously
  only the username was shown. (#biff-nw9)

## [0.8.0] - 2026-02-24

### Added

- **`biff hook` CLI dispatcher** (DES-017) — new `biff hook claude-code <event>`
  and `biff hook git <event>` command groups. All hook logic moves from shell
  scripts to versioned Python. Shell scripts become thin dispatchers with a
  fast `.biff` file-existence gate. (#biff-7vp)
- **Plan auto-expand** — `/plan biff-ka4` now resolves the bead title via
  `bd show --json -q` and expands to `biff-ka4: post-checkout hook`. Falls
  back to the raw string if `bd` is unavailable or the ID is invalid. (#biff-5zq)
- **`plan_source` field** — `UserSession` now tracks how the plan was set
  (`"manual"` or `"auto"`). Manual `/plan` calls always set `"manual"`.
  Git hooks (Phase 2) will set `"auto"` and only overwrite auto plans,
  preventing automated hooks from clobbering intentional plans. (#biff-efk)
- **SessionStart hooks** — on startup, nudges Claude to auto-assign `/tty`,
  set `/plan` from the current git branch (with bead ID expansion), and
  check `/read` for unread messages. On resume/compact, re-orients Claude
  with a `/read` reminder. Branch-inferred plans use `source="auto"` so
  git hooks can later overwrite them. (#biff-6we)
- **SessionEnd cleanup** — on session end, converts active session markers
  (`~/.punt-labs/biff/active/`) to sentinel files for the existing reaper. MCP server
  writes active markers on startup; the hook converts them to sentinels before
  potential SIGKILL, ensuring session presence is cleaned up even on abrupt
  termination. (#biff-w5c)
- **Git post-checkout hook** — on branch switch, writes a plan hint file
  (`~/.punt-labs/biff/plan-hint`) with the expanded branch name (including bead ID
  resolution). The PostToolUse Bash handler picks up the hint and nudges
  Claude to set the plan with `source="auto"`. Switching to main/master
  clears the plan. (#biff-ka4)
- **Git post-commit hook** — after each commit, writes a plan hint with
  `✓ <subject>` so teammates see commit progress in `/finger` and `/who`.
  Uses the same plan hint file mechanism as post-checkout. (#biff-crz)
- **Git pre-push hook** — when pushing to main/master, writes a wall hint
  file (`~/.punt-labs/biff/wall-hint`). The PostToolUse Bash handler picks up the
  hint and suggests `/wall <summary>`. Silent for feature branch pushes.
  (#biff-9e7)
- **Git hook deployment** — `biff enable` deploys post-checkout, post-commit,
  and pre-push hooks into `.git/hooks/`. `biff disable` removes them. Hooks
  coexist with existing git hooks (e.g. beads post-merge) via marked blocks.
  `biff doctor` reports missing hooks. (#biff-9z2)

### Changed

- **Migrated bead-claim and PR-announce hooks** — `bead-claim.sh` (55 lines)
  and `pr-announce.sh` (55 lines) replaced by `post-bash.sh` and
  `pr-announce.sh` thin dispatchers (4 lines each) plus Python handlers
  in `hook.py`. (#biff-7vp)

### Fixed

- **plan_source priority enforcement** — auto plans (from git hooks) can no longer
  overwrite manual `/plan` entries. The guard was documented but not implemented;
  both Copilot and Cursor caught this independently.
- **SessionEnd repo_name mismatch** — `handle_session_end()` now uses the same
  sanitized repo slug as `write_active_session()` (e.g. `punt-labs__biff`), fixing
  a comparison that silently prevented session cleanup when a git remote was
  configured.
- **Branch regex false positives** — `_BEAD_BRANCH_RE` now uses word boundaries
  (`\b`), preventing common branch names like `my-feature` from being truncated
  to `my-feat` and misidentified as bead IDs.
- **Hint file session race** — plan and wall hint files are now scoped by git
  worktree path (`~/.punt-labs/biff/hints/{hash}/`). Multiple sessions in different
  worktrees no longer race on shared hint files. Sessions in the same worktree
  share hints by design — the coordination contract requires worktree isolation.
- **Hint content escaping** — branch names and commit subjects containing double
  quotes no longer break the `/plan with message="..."` prompt syntax. Content is
  now JSON-escaped before embedding.

## [0.7.0] - 2026-02-24

### Changed

- **Shared NATS streams** — consolidated per-repo streams into 3 shared streams
  (`biff-inbox`, `biff-sessions`, `biff-wtmp`) with subject-based repo isolation.
  Removes the 8-repo limit imposed by Synadia Cloud's 25-stream cap. (#62, #64)
- **Idempotent stream provisioning** — create-or-update replaces
  delete-and-recreate, preventing accidental data loss in shared streams (#62)
- **Scoped purge** — `purge_data()` uses subject filters to purge only the
  current repo's data, not the entire shared stream (#62)

### Added

- **Wtmp schema versioning** — `SessionEvent.version` field enables
  forward-compatible schema evolution for the 30-day retention wtmp stream (#64)
- **Stream namespace isolation** — `stream_prefix` parameter on `NatsRelay`
  separates test streams (`biff-dev-*`) from production (`biff-*`) (#64)
- **Encryption extension points** — reserved KV key namespaces (`key.*`,
  `team-key`) and model fields (`UserSession.public_key`, `Message` encryption
  envelope) for future E2E encryption (biff-lff). No encryption code yet. (#62)

### Fixed

- **Resilient consumer cleanup** — `delete_session()` suppresses `TimeoutError`
  and `NatsError` during consumer deletion; `inactive_threshold` is the safety
  net (#64)
- **Legacy stream cleanup** — startup migration deletes orphaned per-repo streams
  with error suppression to avoid crash on first boot after upgrade (#63)

## [0.6.0] - 2026-02-23

### Added

- **MCP server** — FastMCP-based server with HTTP and stdio transports,
  serving tools modeled on BSD commands: `write`, `read_messages`, `finger`,
  `who`, `plan`, `mesg` (#5, #11)
- **Data models** — frozen pydantic models for messages, sessions, and
  presence (#3)
- **Storage layer** — JSONL message store and JSON session store (#4)
- **Relay protocol** — pluggable relay abstraction with `.biff` config file;
  `LocalRelay` for single-machine use (#15)
- **NATS relay** — `NatsRelay` with JetStream messaging and KV-backed
  sessions, automatic relay selection based on `relay_url` config (#16, #17)
- **Remote NATS** — token, NKey seed, and credentials file auth via `.biff`
  `[relay]` section; TLS via `tls://` URL scheme; automatic reconnection with
  disconnect/reconnect/error logging (#19)
- **Dynamic descriptions** — `read_messages` tool description updates with
  unread count and preview after every tool call; fires `tools/list_changed`
  notification (#13, #21)
- **Status bar** — `biff install-statusline` / `biff uninstall-statusline` CLI
  commands that configure Claude Code's status line with per-project unread
  counts and register the biff MCP server (#14, #22, #23, #24)
- **Claude Code plugin** — slash commands (`/who`, `/read`, `/finger`,
  `/write`, `/plan`, `/on`, `/off`) with PostToolUse hook for formatted
  output (#29, #30, #31)
- **Unix-style output** — columnar table format for `/who` and `/read`,
  BSD `finger(1)` layout for `/finger`, with `▶` header alignment for
  Claude Code UI (#31)
- **GitHub identity** — resolve display name from `gh api user` for
  `/finger` output (#27)
- **Team broadcast** — `/wall` command for time-limited team announcements modeled
  after BSD `wall(1)`. Posts a banner visible on every teammate's status bar and
  tool descriptions. Duration-based expiry (default 1h, max 3d), lazy expiry on
  read, three modes: post, read, clear (#biff-klz)
- **Session history** — `/last` command showing login/logout history modeled
  after Unix `last(1)`. NATS wtmp stream (JetStream, 30-day retention) records
  session events. Three-layer logout: sentinel-based (SIGTERM), orphan detection
  (crash recovery at startup), and KV watcher (TTL expiry). Per-user filtering,
  configurable count, columnar output with duration (#49)

### Testing

- **Integration tests** — two MCP clients over `FastMCPTransport` testing
  tool discovery, presence, and cross-user state (#7, #8)
- **Subprocess tests** — real `biff` subprocesses over `StdioTransport`
  verifying wire protocol, CLI args, and cross-process state (#9)
- **NATS E2E tests** — two MCP servers sharing a local NATS relay covering
  presence, messaging, and lifecycle (#18)
- **Hosted NATS tests** — same scenarios against Synadia Cloud or self-hosted
  NATS with weekly CI workflow (#20)
- **SDK tests** — Claude Agent SDK acceptance tests with real Claude
  sessions (#10)
- **Transcript capture** — `@pytest.mark.transcript` auto-saves human-readable
  transcripts to `tests/transcripts/`

### Fixed

- **Notification delivery** — fire `tools/list_changed` when description
  mutates so Claude Code picks up unread count changes (#21)
- **MCP config path** — use `~/.claude.json` (not `~/.claude/mcp.json`) for
  global MCP server registration (#25)
- **MCP server entry** — include required `type` field in server config (#26)
- **jq null guard** — `get_github_identity` filters null `.login` before
  processing (#31)

### Changed

- **Command vocabulary** — renamed tools to match BSD names: `biff` → `mesg`,
  `send_message` → `write`, `check_messages` → `read_messages` (#6, #12, #30)
- **CI** — added pyright to lint workflow (#28)
