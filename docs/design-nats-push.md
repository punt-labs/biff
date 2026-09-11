# Design: NATS Push for Message/Talk Description Refresh (biff-5ex)

Status: **implemented**, including a consolidated fix round (§6) that
corrected two latency regressions a five-agent local-review sweep found
against the original implementation of this design.
Scope: the server-side surfacing path (`set_poll_interval` → tool-description
mutation → `tools/list_changed`). The model-side `/biff:read` cron and the
client-wake model (`tools/list_changed` on next activity) are unchanged.

Sections 1-5 below are the original pre-implementation design and are kept
as written — they remain an accurate description of the mechanism's shape.
§6 documents where the first implementation diverged from that shape under
review and how the fix round closed each gap.

## 1. The current path

### What `set_poll_interval` controls

`set_poll_interval` (`src/biff/server/tools/poll_config.py:55`) writes
`poll_interval` to `config.local.yaml` and requires a restart. The value it
sets feeds a single number, `interval`, into `poll_inbox()`
(`src/biff/server/tools/_descriptions.py:816`), which loops forever:

```python
while shutdown is None or not shutdown.is_set():
    ...
    await asyncio.sleep(interval)          # or wait_for(shutdown.wait(), interval)
    ...
    if not tracker.napping and tracker.idle_seconds() > idle_threshold:
        tracker.enter_nap()
    cheap_nap = tracker.napping and tracker.seconds_since_nap_poll() < nap_interval
    if not cheap_nap:
        last_count, last_wall, last_talk = await _safe_tick(...)   # -> _active_tick
```

(`_descriptions.py:816-898`). `_active_tick` (`_descriptions.py:736-786`) does
three unconditional relay round-trips every tick, active or napping:

1. `state.relay.get_unread_summary(state.session_key)` — unread-message count
   (`_active_tick`, `_descriptions.py:747`).
2. `state.relay.get_wall()` — wall content and countdown
   (`_descriptions.py:760`).
3. `state.talk.expire_stale_invites()` — a pure in-memory sweep, no relay call
   (`_descriptions.py:773`).

Each changed value drives a description mutation:
`refresh_read_messages(mcp, state)` (`_descriptions.py:398-447`),
`refresh_wall(mcp, state, wall=current_wall)` (`_descriptions.py:450-537`), and
`refresh_talk(mcp, state)` (`_descriptions.py:608-626`) if `talk_signal`
changed. Each of those three functions ends with the same idiom:

```python
old_desc = tool.description
tool.description = <new text>
if tool.description != old_desc:
    await notify_tool_list_changed()
```

`notify_tool_list_changed()` (`_descriptions.py:228-339`) is the belt/suspenders
send: belt (inside a tool handler, `ctx.send_notification`) or suspenders
(background poller, `_session.send_tool_list_changed()`), each guarded by
`_notify_lock` and `_NOTIFY_SEND_TIMEOUT`, with `_pending_notify` recording an
unrecoverable drop for the next flush opportunity (`capture_session`,
`_descriptions.py:123-166` — this is the biff-ue2 hardening already landed and
must not regress).

### Which marker is already push-driven, and which is poll-driven

**Talk is already push-driven**, and has been since biff-9la. `poll_inbox`
opens one always-on core-NATS SUB at startup
(`subscribe_talk`, `_descriptions.py:652-705`) on
`NatsRelay.talk_notify_subject(session_key)` — `{stream_prefix}.talk.notify.
{user}:{tty}` (`src/biff/nats_relay.py:1232-1249`). `_on_talk_msg`
(`_descriptions.py:676-696`) feeds every frame into `state.talk.receive()` and,
if that returns `True`, calls `state.activity.wake()` — **never** a direct
`refresh_talk`/notify from the callback, because "sending from a NATS callback
is unreliable" (`_descriptions.py:691-694`, restated at `app.py:600-604` for
wall). The wake only shortens the wait until the *next* poller tick, which
then does the actual `refresh_talk` + notify. So talk's *detection latency* is
already NATS-push (sub-second), even though its *notify delivery* still rides
the poller's tick loop, exactly like wall and messages.

**The message inbox's unread *count* is always computed by a poll** —
`get_unread_summary()` calls `js.stream_info(self._stream_name,
subjects_filter=...)` (`nats_relay.py:1510-1550`), a JetStream metadata query,
routed through `_tracked` (`nats_relay.py:587`). There is no JetStream
"notify on new message" primitive biff uses; `stream_info` must be asked.

**But the message inbox's *wake timing* is already partially push-driven**, and this is
easy to miss: `NatsRelay.deliver()` (`nats_relay.py:1265-1333`) calls
`_publish_talk_notification(message.to_user, message, sender_key)`
unconditionally at the end of *every* delivery — message or talk — publishing a
lightweight payload (`from`/`body`/`to_key`, no `type` field) on the **same**
`talk_notify_subject` the talk SUB listens on
(`_publish_talk_notification`, `nats_relay.py:1335-1378`), but **only when
`":" in to_user`** (targeted delivery; line 1359-1360 returns early for a bare
username). `TalkNotification.from_payload` classifies a payload with no
recognized frame type as a **wake poke** (`talk_types.py:184-219`,
`is_wake_poke` at `talk_types.py:304`), and `TalkState.receive()`
(`talk_state.py:237-266`) diverts it *before* the session-scope filter: it
wakes the poller (`return True`) but is never enqueued as a talk message
(`talk_state.py:245-258`). So **targeted messages already get a NATS wake poke
today** — it just still has to wait for the poller's next tick to actually
recompute the count via `stream_info()` and notify.

**What is genuinely poll-only, with zero push signal**: broadcast messages
(`to_user` with no `:tty`, e.g. `/write alice "..."` with no active session
addressed) — `_publish_talk_notification` returns before publishing anything
for it (`nats_relay.py:1359-1360`). Its arrival is invisible until the next
scheduled tick, whatever `poll_interval`/`nap_interval` happens to be at that
moment.

**Wall is fully push-driven for detection** via the KV watcher
(`_run_kv_watch`, `app.py:570-608`): a `PUT`/`DEL` on the `{repo}.wall` KV key
calls `state.activity.wake()` (`app.py:604`). But the poller *also*
unconditionally re-fetches `get_wall()` every tick regardless of any push,
because the countdown text ("expires in Ns") must keep ticking down even with
no underlying change — this is out of scope for biff-5ex and is the reason
the tick loop cannot simply be deleted (§3).

**Summary table**:

| Marker | Detection | Notify delivery | Poll-only gap |
|---|---|---|---|
| Talk (invite/message/end/withdraw) | Push (always-on SUB, biff-9la) | Poller tick (belt/suspenders) | None |
| Message, targeted (`user:tty`) | Push (wake poke on talk subject, piggybacked on `deliver()`) | Poller tick | None for *detection*; count itself is `stream_info()` |
| Message, broadcast (`user`) | **Poll only** | Poller tick | Full poll_interval/nap_interval latency |
| Wall | Push (KV watcher) | Poller tick | Countdown re-render only, not detection |

## 2. Proposed push mechanism

### Choice: extend the existing wake-poke pattern, not a new consumer

Three alternatives were considered:

- **New JetStream consumer per session watching the inbox stream.** Rejected:
  JetStream consumers are metered server-side resources (biff already avoids
  creating one for `get_unread_summary`, DES-015, precisely to keep
  `stream_info` zero-consumer); a consumer per live MCP session reintroduces
  the scaling concern DES-015 closed, for a signal that only needs to say
  "something arrived," never the payload.
- **KV watcher fan-out** (mirror wall). Rejected for messages: the message stream is JetStream
  WORK_QUEUE, not KV — there is no KV key that changes on message arrival, and
  inventing one (a per-user/tty "last-delivered" KV counter written on every
  `deliver()`) adds a KV write to the hot send path and a new namespace to
  reason about, for no benefit over the mechanism already proven for talk.
- **Core NATS wake poke, generalizing what `deliver()` already does for
  targeted messages.** **Chosen.** Reuses a pattern already shipped, reviewed, and
  spec'd (biff-9la); needs one new subject and one new always-on SUB
  (parallel to `subscribe_talk`), not a new consumer class.

### Subjects

- **Targeted messages** (`user:tty`): no change — already covered by
  `talk_notify_subject`. Zero new code.
- **Broadcast messages** (`user`, no tty): new core-NATS subject
  `{stream_prefix}.{repo}.notify.{user}`, one per repo per user,
  fanning out to every live MCP session for that user in that repo
  (multiple terminals of the same user each subscribe independently;
  core NATS delivers to all current subscribers, which is the correct
  semantic — any of that user's sessions in the repo might want to wake
  and re-check). The subject is repo-scoped, not repo-less, and
  deliberately so: DES-048's identity-routed, repo-less discipline
  applies to *targeted* delivery, where `user:tty` is a globally-unique
  identity (`talk.tex`, `subjectOf k = k`); a broadcast poke names a
  bare `user`, which is *not* a unique identity, and it announces
  activity on the repo-partitioned durable inbox `biff.{repo}.inbox.
  {user}` (DES-030: bare-user addressing is repo-local), which only
  that repo's sessions can read. A repo-less subject would wake every
  repo's sessions of that user for an inbox most of them cannot see —
  spurious recomputes and a cross-repo activity leak. The subject is
  scoped to the same repo as the durable subject it signals, but does
  **not** reuse its `inbox` token: the durable inbox stream is
  provisioned with the wildcard filter `{stream_prefix}.*.inbox.>`
  (`_provision`, `nats_relay.py:924`), so a poke subject containing
  `inbox` as its third token is silently captured into the shared
  JetStream WORK_QUEUE stream — no consumer reads it, and with no
  `max_age`/`max_msgs` bound for this case it sits there forever,
  permanently consuming a slot in the shared 100 MiB budget and,
  over enough broadcasts, evicting real undelivered messages. `notify`
  replaces `inbox` for exactly this reason, mirroring
  `talk_notify_subject`'s own stream-safe shape. Verify a poke subject
  against the stream's *filter*, not by comparing literal subject
  strings — a subject can be distinct from every other subject in use
  and still collide with a wildcard.
  `deliver()`'s broadcast branch (`nats_relay.py:1316-1330`) publishes a
  bare wake byte (`b"1"`, same fallback `_publish_talk_notification`
  already uses when there is no `Message`) to this subject after the
  JetStream publish succeeds, mirroring the targeted branch's existing
  call.
- **Wall**: unchanged (KV watcher already covers it).
- **Talk**: unchanged (`talk_notify_subject`, unchanged wire format).

### Delivery semantics

Core NATS, not JetStream: **at-most-once, fire-and-forget, no redelivery, no
ordering guarantee**, identical to the existing talk-notify subject. This is
deliberate and matches talk's own documented semantics ("notifications ride
NATS core pub/sub with no durable inbox. A dropped notification is simply
lost," `talk_state.py:11-13`). It is safe here specifically *because* the
wake poke never carries the actual unread state — it only shortens the time
until the next `stream_info()`/`refresh_read_messages()` pass. If a poke is
dropped (subscriber briefly reconnecting, mid-wedge), three independent
recovery paths already exist and need no new code:

1. The belt path: `refresh_read_messages` already runs after *every* tool
   call succeeds (`_descriptions.py` module docstring, line 4: "Called after
   every tool execution (belt)"), so the next tool call the agent makes
   re-syncs the count regardless of whether any push arrived.
2. The retained low-frequency tick (§3) still exists. `get_wall()` keeps
   running on it unconditionally, every tick, whether or not a poke
   arrived — see §3, this is load-bearing for wedge detection, not
   optional. `get_unread_summary()` is **poke-gated with its own
   backstop**, not unconditionally retained: a poke marks a gate that
   `_active_tick` reads (recompute now); absent a poke, the same gate
   still forces a recompute once `nap_interval` has elapsed since the
   last one, so a dropped at-most-once poke costs at most one backstop
   interval of latency, never a stalled count. `set_poll_interval` sets
   that backstop interval (§5).
3. The model-side `/biff:read` cron (out of scope, unchanged) periodically
   calls the tool regardless of description state.

### One push event → exactly one refresh, at most one notify

No new coordination is needed here beyond what already exists, because the
design deliberately does **not** call `refresh_read_messages()` or
`notify_tool_list_changed()` from the NATS callback. The new
`_on_inbox_notify_msg` callback (parallel to `_on_talk_msg`,
`_descriptions.py:676-696`) does exactly two things, both pure bookkeeping:
marks the poke gate (`_InboxPokeGate.mark()`, §3) so the next tick's unread
recompute is not deferred to the backstop, and calls `state.activity.wake()`.
Multiple rapid pushes collapse into a no-op re-mark and re-wake
(`ActivityTracker.wake()`, `activity.py:36-50`, is idempotent — it just resets
`_last_nap_poll` to the epoch). The next poller tick — one tick, since the
loop is sequential — calls `refresh_read_messages`, which itself already
enforces the `!= old_desc` change-gate (`_descriptions.py:443`) before calling
`notify_tool_list_changed()`, and that function's own `_notify_lock` and
`_pending_notify` bookkeeping (the biff-ue2 hardening) is untouched. So: one
or more pushes between two ticks produce at most one `refresh_read_messages`
call and at most one `tools/list_changed` — the same guarantee the design
already has for wall and talk, extended to broadcast messages with zero new state machinery
in `_descriptions.py`.

### New subscription bookkeeping (parallel to `TalkSubscription`)

The broadcast inbox-notify SUB needs the same generation-tracked lifecycle as
the talk SUB, because it is the same class of resource: an always-on core-NATS
subscription bound to a particular `nats.connect()` client, which a
force-reconnect (`_force_reconnect`, `nats_relay.py:653-713`) discards
wholesale. Concretely: an `InboxNotifySubscription` `NamedTuple` (handle +
generation), a `subscribe_inbox_notify()` mirroring `subscribe_talk()`
(`_descriptions.py:652-705`), and a `_reconcile_inbox_notify_sub()` mirroring
`_reconcile_talk_sub()` (`_descriptions.py:708-733`), called from
`poll_inbox` alongside the existing `_reconcile_talk_sub` call
(`_descriptions.py:898`). This is new code, not a design change to the
existing talk machinery — but it is new *state the Z spec does not yet
model* (§4).

## 3. The wedge-detection interaction (load-bearing)

### The risk, stated precisely

`_WEDGE_FORCE_RECONNECT_THRESHOLD = 3` (`nats_relay.py:122`) counts
*consecutive* `_tracked()` timeouts. `_tracked` is only exercised by an actual
JetStream/KV round-trip — nats-py's own PING/PONG keepalive does **not** go
through it. Today, three independent loops feed `_tracked`:

1. `poll_inbox`'s `_active_tick`, every `interval` (default 2s) while active,
   `nap_interval` (default 30s) while napping — issues `get_unread_summary()`
   (1-2 `stream_info` calls) and `get_wall()` (one `kv.get`) every tick
   (`_descriptions.py:747,760`, both `_tracked`-wrapped inside
   `nats_relay.py`).
2. `_heartbeat_loop`, every 60s (`app.py:492-533`, default `interval=60.0`) —
   `kv.get` + `kv.put` on the session key (`heartbeat()`,
   `nats_relay.py:1592-1646`), independent of `poll_inbox`.
3. Any tool call the agent happens to make (irregular, not a scheduled
   source).

At the *current* 2s active cadence, three consecutive timeouts (each blocking
~5s, the nats-py JetStream request timeout) take ~15s — the number cited in
`nats_relay.py:117` and the wedge-liveness section of `nats-relay.tex`
(introduction, biff-3hp paragraph). **If `_active_tick`'s
`get_unread_summary()`/`get_wall()` calls are removed outright** (a literal
reading of "eliminate the poll"), the only remaining periodic `_tracked`
source is the 60s heartbeat. Three consecutive timeouts then require three
heartbeat ticks — **worst case ~3 minutes** (60s apart, 5s timeout each) —
*slower than the ~60-80s keepalive floor `ForceReconnect` exists to beat*.
That is not a modest latency regression; it inverts the ordering the whole
proactive-detector design assumes (§ introduction, `nats-relay.tex`: "the
proactive detector beats \[the keepalive\] by tearing the connection down
after three consecutive timeouts... 4-5x faster than keepalive"). A naive
"just remove the poll" implementation would make `ForceReconnect` *slower*
than `WedgeDetected` in the worst case, which the spec's own liveness proof
(§ modelcheck, item 2) never anticipated — the proof shows `halfOpen` cannot
be stuck, not that either detector's *speed* is preserved.

### Recommendation: retain the wall tick, drop only the unread-count poll

The wall countdown text ("expires in Ns") already forces the tick loop and
its `get_wall()` `_tracked` call to keep running at `interval`/`nap_interval`
cadence *regardless of biff-5ex* — the countdown must re-render every tick
whether or not the underlying wall content changed, and that requirement is
explicitly out of scope here. This means the wedge-detection cadence does
**not** need a new dedicated heartbeat: it is already preserved as a side
effect of not touching wall's tick, as long as the implementation removes
only the *unconditional* `get_unread_summary()` call from `_active_tick`,
replacing it with the poke-gated check of §2: recompute when a poke has
arrived, or when `nap_interval` has elapsed since the last recompute
(the backstop, `_InboxPokeGate` in `_descriptions.py`) — and leaves
`get_wall()` running every tick, unconditionally, untouched. No
regression, no new heartbeat — provided this dependency is called out
explicitly so a later refactor doesn't "simplify" the tick loop into
deletion once broadcast messages no longer need it.

This recommendation is conditional, not free: if a future change *also*
removes or slows the wall tick (e.g. moving wall to a fully push-driven
countdown, computed client-side from a timestamp rather than re-rendered
server-side), wedge-detection cadence degrades with it, and the mitigation
below becomes necessary at that point, not before.

### If the tick loop is ever fully removed

Should a future iteration eliminate `poll_inbox`'s tick entirely (not
proposed here), the mitigation is a low-frequency, business-logic-free
`_tracked` call whose only purpose is feeding the wedge counter — e.g. a bare
`kv.get` on a well-known key every 5-10s, decoupled from `poll_interval`/
`nap_interval` and from the 60s heartbeat. Driving the counter off the push
subscriptions' own liveness (e.g., treating a long gap with no core-NATS
traffic as a timeout) was considered and rejected: `_tracked` specifically
measures *request* round-trips, and a subscription receiving nothing is
indistinguishable from "no traffic right now" and "the SUB is silently dead
on a stale client" — exactly the ambiguity `talkSubGen` tracking exists to
resolve structurally, not by inference from silence.

## 4. Reconciliation with the governing Z specs

### `docs/notification.tex` — fits unchanged

The spec models the surfacing state machine (`MailArrive`,
`TalkInviteArrive`, `PollTick`, the belt/suspenders notify split, the
`pendingNotify`/`notifyLost` accounting) entirely in terms of *what changed*
and *how the notify was attempted*, never *how the change was detected*.
`MailArrive` (`notification.tex:2445-2482`) is an abstract "count went up"
transition with no precondition tying it to a poll tick; `PollTick`
(`notification.tex:1415-1454`) already models the idle case where nothing
changed, without asserting that a *poll* — as opposed to a *push-triggered*
tick — is what ran it. Crucially, `KVWallReceive` and `NatsTalkCallback`
(the two schemas modeling exactly wall's and talk's existing push paths) are
already in the spec, and their own commentary (`notification.tex:3465-3467`)
states they "still defer the actual send to the poller" — precisely the
pattern §2 proposes for broadcast messages. A `MessagePushCallback` schema, added the same way
`NatsTalkCallback` was, would be a **mechanical extension**, not a
reconciliation of conflicting model and code — no existing invariant is
falsified by adding a third "wake, defer the send" source. This is a
**spec extension recommended before implementation** (new schema +
its entry in the `NotifyGuarantee`/change-gate proofs), but it is additive,
low-risk, and follows an established template exactly.

### `docs/nats-relay.tex` — needs an extension before implementation

Two distinct additions are needed, both non-trivial because they touch the
proven state space, not just prose:

1. **A second always-on subscription.** `talkSubGen` (`nats-relay.tex:345`)
   and its liveness property (§ modelcheck, item "Talk-subscription
   liveness (biff-9la)") are specific to *one* tracked SUB generation. A
   broadcast inbox-notify SUB is a second instance of the same resource class
   (bound to a client, orphaned by `ForceReconnect`, replayed by an in-place
   `Reconnect`). The model must either generalize `talkSubGen` to a
   finite family of subscription-generation bindings (one per subscription
   kind — talk, inboxNotify, and any future one) with the liveness
   property stated once and quantified over the family, or duplicate the
   schema with an `inboxNotifySubGen` variable and a parallel CTL formula. The
   former is preferable — it is the generalization the model will need again
   for any *third* always-on SUB — but either is a real extension to
   `Connection`'s state schema, not prose. **jms/jra must extend this before
   implementation begins**, per the biff rule that code conforms to the
   proven spec, not the reverse.
2. **The wedge-detection cadence dependency (§3) should be recorded as an
   explicit invariant, not left as an implementation comment.** The spec's
   wedge-liveness proof (§ modelcheck, item 1) shows `halfOpen` cannot be a
   terminal state; it does not currently model the detection-latency
   distinction between `WedgeDetected` and `ForceReconnect` at all — both
   transitions are equally "eventually fires." §3's finding — that removing
   the wrong periodic `_tracked` source can make `ForceReconnect` slower than
   `WedgeDetected`, inverting the design's own stated 4-5x speed advantage
   — is a *timing* property the current model has no vocabulary for (it is
   untimed CTL). Recording it as a design-log invariant ("some periodic
   `_tracked` source must run at ≤ interval cadence for `ForceReconnect` to
   retain its speed advantage over `WedgeDetected`") is the pragmatic
   choice; a timed extension of the Z model (real-time CTL, or an explicit
   tick-count bound) is possible but disproportionate to what this ticket
   needs — flagged as an open question in §5, not a required extension.

## 5. Open questions for the leader/operator

1. **Heartbeat retention.** Recommend: **retain `get_wall()`'s existing
   tick unchanged**; remove only the unread-count poll from `_active_tick`.
   No new heartbeat is needed unless a future change also removes the wall
   tick, at which point a dedicated low-frequency `_tracked` call (§3,
   "if the tick loop is ever fully removed") becomes necessary and should be
   designed as its own follow-up, not bundled into this one.
2. **Delivery-semantics choice.** Recommend: **core NATS, at-most-once,
   fire-and-forget**, identical to the existing talk-notify subject — not a
   JetStream consumer (rejected in §2 on DES-015 grounds) and not a
   KV-watched key (rejected in §2 as a mismatch with the message stream's WORK_QUEUE
   model). The three-layer recovery in §2 (belt path, retained tick,
   model-side cron) makes at-most-once acceptable for a signal that never
   carries the actual data.
3. **Whether `set_poll_interval` is removed outright or kept as a
   fallback.** Recommend: **keep it, but repoint its documented meaning.**
   Once broadcast-message detection is push-driven, `set_poll_interval`'s remaining
   effect is (a) wall-countdown re-render cadence, (b) stale-invite expiry
   cadence, and (c) the wedge-detection cadence dependency of §3 — all
   real, all still user-tunable, none of them "how fast do messages arrive"
   anymore. Removing the tool outright would also remove the only knob an
   operator has to widen or narrow the wedge-detection window, and would
   force a restart-required config edit to recover it later. The tool's
   description (`poll_config.py:49-53`) needs rewording so `n` (disable)
   is understood to also disable wall-countdown refresh and widen
   wedge-detection to the ~60-80s keepalive floor — a behavior change worth
   a CHANGELOG entry at implementation time, not folded silently into "poll
   removed."
4. **Scope of the always-on-SUB generalization (§4, item 1).** Recommend:
   generalize `talkSubGen` to a family now, in the same spec-extension pass,
   rather than duplicating the schema for inboxNotify and re-doing the
   generalization for the next always-on SUB. This is a design-time call
   for jms, not an implementation detail rmh should decide unilaterally,
   since it changes `Connection`'s state schema shape.

## 6. Fix round: latency regressions found by review, and their closure

The first implementation (missions m-009/m-014) built §2's mechanism as
designed but, under a five-agent local-review sweep run afterward, was found
to have quietly regressed two of §1's "already push-driven" paths back to
poll-only behavior, plus several narrower correctness and type-design gaps.
This section documents each gap and the fix, so a future reader does not
have to reconstruct the reasoning from the diff alone.

### Gap 1: targeted messages stopped marking the poke gate

§1 established that a targeted (`user:tty`) message already rides a wake
poke on the talk-notify subject (`_publish_talk_notification`), not a
second poke. The first implementation's poke-gated `_active_tick` (§2,
"One push event → exactly one refresh") correctly gated the unread
recompute behind `_InboxPokeGate`, but `_on_talk_msg` — the talk SUB's
callback — only called `state.activity.wake()` for a wake poke, never
`gate.mark()`. Once the recompute became gate-conditional instead of
unconditional-per-tick, a targeted message's detection latency silently
fell from "next tick" to "next backstop interval" (up to `nap_interval_for
(poll_interval)`, 15x the configured interval) — a real regression against
both the pre-biff-5ex baseline and this design's own stated intent.

Fix: `_on_talk_msg` now classifies each frame via
`TalkNotification.from_payload(frame).is_wake_poke` and calls `gate.mark()`
for a wake poke specifically — never for a genuine talk frame (invite,
message, end, withdraw), where marking the inbox gate would be a spurious
unread recompute unrelated to what actually changed.

### Gap 2: a dual session's companion had no inbox-notify SUB at all

§2 designed one inbox-notify SUB per session, bound to
`inbox_notify_subject(repo, state.config.user)`. A dual session (DES-039)
has a second identity, `state.companion`, whose broadcast messages land on
`inbox_notify_subject(repo, state.companion.user)` — a subject the single
SUB never subscribed to. A broadcast addressed to the companion therefore
had zero push signal, regressing it to the backstop exactly as gap 1 did
for targeted messages, but for a different reason (a missing subscription,
not a missing gate-mark).

Fix: `poll_inbox` opens a second, independent inbox-notify SUB — same
callback shape, same gate, bound to `state.companion.user` — whenever
`state.companion is not None`. `subscribe_inbox_notify` and
`_reconcile_inbox_notify_sub` gained a `user` parameter so the *same*
functions serve both bindings; no companion-specific code path was added.
`docs/nats-relay.tex`'s `SubKind` free type gained a third constructor,
`inboxNotifyCompanion`, to keep the two live SUBs' generation-tracking
distinguishable in the model — every operation already quantified over
`SubKind` needed no further change (see the spec's own commentary at the
`SubKind` declaration).

### Gap 3: `set_poll_interval n` silently amputated all push

The poller task itself — which hosts every always-on SUB — was only
created when `poll_interval > 0`. Disabling polling therefore disabled
talk, targeted, broadcast, and companion push alike, while the tool's own
description claimed messages and talk "arrive in real time via NATS push
regardless of this value." Fix: the poller task now always runs. Its
sleep-or-wake step is a shared `asyncio.Event` rather than a plain
`asyncio.sleep(interval)`, so a wake (talk activity, a wake poke, or a
broadcast poke) interrupts the wait immediately at any interval, including
`interval <= 0` (which waits indefinitely on the event alone, with no
periodic timeout). What actually still degrades at `interval <= 0` is the
*periodic* work that has no push signal of its own — the wall countdown's
re-render, stale talk-invite expiry, and the backstop — because with no
periodic wake, that work only runs on whatever tick a poke happens to
produce. `set_poll_interval`'s tool description and its `n` response text
were rewritten to state this distinction honestly, including the real 15x
backstop ratio the original text glossed over as "on this cadence."

### Gap 4: the poke could be clobbered by a same-tick failed refresh

`_InboxPokeGate`'s two-method form (`should_recompute` / `recompute_done`)
let a caller clear the poke and only later discover the refresh it gated
had failed, with no way to signal "try again next tick" back to the gate.
Collapsed into one atomic `claim()`: it clears the poke and resets the
backstop clock in the same synchronous step as the check, before the
caller's own `await` on the refresh — so a poke arriving mid-refresh
survives to the next tick rather than being silently absorbed — and
`refresh_read_messages` now reports fetch success/failure back to its
caller so `_active_tick` can call `gate.mark()` again on a failure,
re-arming the very next tick instead of waiting out a full backstop for a
transient error.

### Narrower findings closed in the same round

- `_live_nc_or_reconnect`'s fall-through to a fresh dial was unbounded
  inside a caller documented as best-effort and near-instant; wrapped in
  `asyncio.timeout(_NOTIFY_RECONNECT_TIMEOUT)`.
- `NatsRelay` gained a terminal `_closed` flag, set only by `close()`
  (never the reversible `disconnect()`), checked by
  `_live_nc_or_reconnect` so a poke firing after close cannot resurrect a
  connection nothing will ever close again.
- `_validate_user` now rejects `:` — `inbox_notify_subject`'s disjointness
  from `talk_notify_subject` was already documented as depending on a bare
  user never containing one, but nothing enforced it.
- Type-design cleanup: `SubscriptionBinding.handle` is a small
  `_Unsubscribable` Protocol instead of `object` with `type: ignore`
  comments at each call site; the `TalkSubscription`/`InboxNotifySubscription`
  aliases were deleted in favor of the one `SubscriptionBinding` NamedTuple
  every always-on-SUB kind already shared structurally;
  `_InboxPokeGate` uses `time.monotonic()` instead of wall-clock time, so a
  backward NTP step cannot suspend the backstop.
