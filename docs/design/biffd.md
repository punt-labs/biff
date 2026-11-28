# biffd: the Per-User Daemon

Status: proposed design (DES-095). Migration step 1 is built: `biff daemon
serve`, the frames and the session records below. Step 2 is in progress
(DES-099): the `biff` product commands (`who`, `finger`, `write`, `read`,
`plan`, `last`, `wall`, `mesg`, `tty`, `status`) route through the daemon;
`watch`, the `session` verbs, `talk` and the REPL, `biff mcp` and the hooks
do not yet.

`biffd` is one long-running process per operating-system user. It owns every
connection and every piece of state that biff's clients would otherwise
duplicate. The `biff` command, the `biff mcp` server, the hooks and the
harness plugins become thin clients that talk to it over a local connection.

This document owns biffd's design. Keys, signing and message trust are in
[trust-model.md](trust-model.md) and are not restated here. Package placement
follows [package-structure.md](package-structure.md) (DES-088).

Terms used below:

- **Client**: any process that talks to biffd: `biff mcp`, the `biff` command,
  a hook, a harness plugin.
- **Harness**: the AI coding tool a session runs in: Claude Code, Codex,
  opencode, pi, Devin.
- **Arrival summary**: the short record the engine produces when something
  reaches a session (mail, wall, talk invite, talk message). It carries no
  message body beyond what the delivery policy allows.
- **Relay**: the NATS server biff messages travel through, or the local relay
  for a repository that has no NATS configuration (DES-090).

## What biffd owns

| State or duty | Why it is in the daemon |
|---------------|-------------------------|
| The relay connections and their reconnect lifecycle | One connection per relay target (server, credentials and repository), however many sessions use it. Today every `biff mcp` process opens its own. |
| Inbox and unread state | Several clients read and mark the same unread set; read-once semantics need one writer. |
| Presence and `.plan` | One heartbeat per session, sent by the daemon, not by each client. |
| Talk sessions | A talk has two ends and a queue (`docs/specs/talk.tex`); one owner keeps the queue bounded. |
| Notification fan-out | The daemon produces each arrival summary once and sends it to every subscribed client. |
| Session records | Per [mcp-caller-identity § 4.2](https://github.com/punt-labs/punt-kit/blob/main/standards/mcp-caller-identity.md): get-or-create by session id, live, dormant or ended. |
| Private keys and signing | biffd signs on behalf of clients; a client gets a token, never a key. Custody, attestation and verification are specified in [trust-model.md](trust-model.md). |
| Message semantics and delivery policy | Identical for every harness (R-D.4): classification, read-once behavior and what a harness may show live in the engine, so an adapter cannot differ. |

One relay connection serves every session that targets the same relay. The
target is the key `(server, credentials, repository)`: the NATS relay scopes
its subjects, presence and wall by repository name, so two repositories on one
NATS server are two targets, and sharing one NATS client between them is a
later optimization. A repository with the local relay
(`.punt-labs/local/biff/relay/`, DES-091) is its own target, so biffd holds one
connection per distinct target and the sessions of one repository share it.

## What stays in clients

A client holds no relay connection and no private key, with one named exception:
`biff wall` in a process whose environment carries `BIFF_RELAY_*` settings (a CI
runner posting a build failure, with no session, inbox or daemon) posts, shows
or clears the wall on the relay it was handed, on its configured repositories
and those of its organizations, and closes it. No other command has a direct
path (DES-099). It sends requests,
receives replies and events, and renders them.

| Client | Role |
|--------|------|
| `biff mcp` | A per-session stdio MCP server, as `vox mcp` and `quarry mcp` are. It translates each tool call into a daemon request. It sends Claude Code's channel events (DES-087), because only this process holds the stdio pipe to that session. |
| `biff` command and `biff watch` | One request per invocation. `biff watch` subscribes and prints one JSON line per arrival summary (PL-235). |
| Hooks (`biff-hook`) | Short requests: register a session, read unread counts, end a session. Each must finish within the hook's time limit, so each uses the daemon-absent rules below. |
| Harness plugins | The TypeScript plugins for opencode and pi subscribe through `biff watch` or the client protocol and call their host's API. |

The MCP server stays a client and is not served from inside the daemon. luxd
serves MCP over HTTP inside its process; biff cannot, because MCP caller
identity (`_caller`, stamped by a Claude Code hook) is per session and arrives
on that session's own stdio connection.

## Connection and protocol

Decision: **a WebSocket on `127.0.0.1:8424` carrying JSON frames, with a bearer
token read from a file.** This is voxd's transport.

| Fact | Value |
|------|-------|
| Listener | `127.0.0.1:8424`, loopback only; `--port` overrides it |
| Token file | `~/.punt-labs/biff/run/serve.token`, mode 0600, created once and stable across restarts |
| Port file | `~/.punt-labs/biff/run/serve.port`, written at start, removed on clean shutdown |
| Health | `GET /health` on the same listener, unauthenticated, returns the daemon version and uptime only |
| Paths | Under `~/.punt-labs/biff/` per filesystem standard FS-1; `biff install` creates `run/` at mode 0700 (DES-091) |

**Why a port, a token and a WebSocket.** The three sibling daemons settled
this: voxd uses a WebSocket on 8421 with `serve.token`; quarryd uses REST on
8420 and requires its token on loopback; luxd serves on 8430. A loopback port
lets `punt-service` guard it, probe it and report it in `doctor` with the code
the siblings already use. A WebSocket carries requests, replies and server
pushes on one connection, so the arrival subscription needs no second
channel. Quarry's REST has no push, which is why biff follows voxd.

**Why 8424.** 8420, 8421 and 8430 belong to quarryd, voxd and luxd. Quarry's
wheel test and vox's tests bind 8422 as a scratch port, so a developer running
them would collide with a daemon on 8422. 8424 is the next number in the
8420 to 8430 block that no repository uses.

**What the token proves.** Any process of the same operating-system user can
read the token file, so the token authenticates "the same user", not "this
agent". Separating an agent from the keys, and one agent from another, is the
job of the per-client tokens and signing rules in
[trust-model.md](trust-model.md).

### Frames

Every frame is a JSON object with a protocol `version`.

| Frame | Direction | Content |
|-------|-----------|---------|
| `challenge` | client to daemon, first frame | `client_nonce`: 128 random bits, fresh per connection. Nothing secret |
| `proof` | daemon to client | `daemon_nonce` (128 random bits, fresh per connection, used once) and `mac` = HMAC-SHA256(token, `biffd-daemon-v1` + `client_nonce` + `daemon_nonce`), as hex |
| `hello` | client to daemon, after the proof checks | `mac` = HMAC-SHA256(token, `biffd-client-v1` + `daemon_nonce` + `client_nonce`), as hex, in place of any token; client type (`mcp`, `cli`, `hook`, `plugin`), and the caller facts: `session_id`, `main_agent_id` (empty when the harness states none), `cwd` (the directory the client runs in, which names the repository and so the relay target) and an optional `user` override. The daemon answers a valid hello with a `reply` whose `id` is 0 |
| `request` | client to daemon | `id`, `method`, `params`. One `method` per command object in `commands/` |
| `reply` | daemon to client | `id` and either `result` (plain `wire` data) or `error` (a code and an actionable message). A command's `result` is `{"data": <the command's JSON payload>, "error": <bool>}` |
| `ack` | client to daemon | `id` of a request. Sent after the client received that request's reply, when the reply set `needs_ack` (a `read` that carries mail). The daemon marks the mail read only on this frame |
| `subscribe` | client to daemon | `id`. Starts the arrival subscription for the connection's session. The daemon replies to `id` with `{"subscribed": true}`, or with `already_subscribed` or `session_replaced`. A `read` of an ended session is refused with `session_ended` |
| `event` | daemon to client | One arrival summary, with a per-daemon `seq`. `kind` is `unread`, `mail`, `wall`, `degraded` (the relay stopped answering), `recovered`, or a talk notice: `talk_invite`, `talk_accept`, `talk_line`, `talk_end`. A talk notice carries `from`, `tty` and `pending` (frames waiting), never text; the client pulls the text with `talk_read` |

**The token never crosses the wire.** The client's port is a guess (8424
when no port file exists), and any local process can bind a port biffd is not
using, or reach the real daemon on loopback. So neither side hands the other
the token or answers for it without proof of its own:

1. The client sends `challenge{client_nonce}`.
2. The daemon sends `proof{daemon_nonce, mac}`, where the MAC covers the label
   `biffd-daemon-v1`, then both nonces, under the token. The daemon makes
   `daemon_nonce` for this connection and keeps it nowhere else.
3. The client checks the MAC with `hmac.compare_digest` against the one it
   computes from its own token file. A missing, late or wrong proof ends the
   attempt with an error naming the port and saying no credential was sent.
4. The client sends `hello{mac, ...}` with a MAC over the label
   `biffd-client-v1`, then `daemon_nonce` and `client_nonce`.
5. The daemon checks it with `hmac.compare_digest`. A wrong MAC closes the
   connection (1008) and changes no state.

The labels differ and the nonces are in opposite orders, so neither side's MAC
can be reflected back as the other's. A MAC binds both nonces, so it is good on
one connection and is refused on any other: an impostor that records a hello
cannot replay it, and an impostor that cannot compute the daemon's MAC never
receives a hello at all. The daemon's answer to a challenge is not a signing
oracle for a client MAC, because the two MACs use different labels and the
daemon never signs the client label.

**Residual.** An attacker that holds the port can still sit in the middle of a
live, correctly proven connection by relaying both directions: it forwards the
challenge to the real daemon and the proof and hello back and forth. It learns
no token and no credential that works on another connection, but while it
relays it can read and alter that connection's frames. Closing that needs a
channel the attacker cannot sit on, a Unix socket with peer-uid checks or TLS.
That is a separate design decision for DES-095, not made here.

The client also reads the token only from a run directory at mode 0700 and a
token file at mode 0600, both owned by the current user, and refuses otherwise
with the `chmod` that repairs it.

A frame other than a `challenge` first, a `hello` before the proof, a second
`hello`, a request before a valid `hello`, or a wrong MAC closes the
connection and changes no state. A `hello` whose `version` the daemon does
not support gets an error naming both versions and the command that fixes the
mismatch. Until `biff daemon restart` exists it says to stop the running
`biff daemon serve` and start it again, or upgrade the client. Both sides read
`version` from the raw object before validating the rest of the frame, so a peer
whose frames have another shape is still named, not closed without a word.

Timeouts: the client waits 1 second to open the connection and 5 seconds for
the proof and for the hello's reply, and each request waits 10 seconds by
default; a miss is an error naming what timed out. The daemon answers requests
concurrently, each reply carrying its request's `id`, and gives a hello 10
seconds and a request 30 seconds before it replies `internal` naming the relay.
A connection whose run a newer run replaced is closed with 1008 and the reason
"session replaced by a newer run; reconnect".

Results are `wire` data and never rendered text. The client runs `render`, so
`biff who` prints the same table whether the command ran in the daemon or in
today's single process.

### Delivery of mail

A reply that carries mail is not the end of its delivery. A socket write says
the bytes left the daemon, not that the client read them: a client that timed
out discards a late reply. So the daemon marks the mail read only on the
client's `ack` for that request. A connection that ends before the `ack` leaves
its mail unread, and the next `read` serves it again.

A relay that consumes mail when it is fetched (NATS) has nothing left to mark,
so the daemon holds fetched mail in the session's outbox until the `ack`. A
relay that does not consume (the local relay) keeps unread mail itself, and its
broadcast inbox is shared by every session of the user, so its outbox holds
nothing: each `read` is built from what the relay says is unread now, and a
message another session marked read is never served again. `more` counts the
mail a batch leaves: held mail, plus what a consuming relay still holds behind
its bounded fetch (NATS pulls at most 100 messages per inbox at a time). The
reconnect summary counts held mail as unread.

Two residuals remain, both at-least-once delivery rather than loss:

- An abandoned read still consumes mail from a consuming relay. The client gave
  up, so the mail is not delivered, but it has left the relay; it waits in the
  outbox and the next `read` serves it.
- Mail held in the outbox is lost if the daemon dies first. It had already left
  the NATS stream, as mail a client fetched is lost if the client dies. The
  outbox is memory, not a log.

### Subscription

`subscribe` replaces the in-process `DeliverySink` for out-of-process
consumers. On subscribe the daemon first sends the session's current unread
summary, then, when the repository has an active wall, one `wall` event for it,
then one `event` per later arrival. There is no replay log: a client
that reconnects after a daemon restart gets the current summary again. Mail is
durable, so a client that never polls loses none of it across a disconnect
(R-D.5, R-OC.2). A wall is a current value, one active post per repository:
the client recovers the active wall, and a wall that a newer one replaced
during the disconnect is not recovered. Talk frames are ephemeral: a frame that arrives while
the daemon is down is not recovered, as with today's relay reconnect.

### Talk

A talk is a conversation, so it is three methods and four notices, not one
request and reply. The daemon holds each session's `TalkState` and, while a
client is attached, one NATS subscription that feeds it (`TalkFeed`); the
subscription is released when the last client detaches and re-made when the
relay dials a new client (`nats-relay.tex` `talkSubGen`). `talk.tex` bounds the
queue at 100 frames with drop-oldest, and the daemon keeps that bound and counts
what it drops. An invite carries no text; the opening line of `talk` waits in the
talk state and goes out as a normal line once the accept arrives. When the last
client of a session detaches mid-talk the daemon hangs up (end or withdraw, best
effort) and resets, so a client that died leaves neither a peer waiting nor a
session refused as already in a talk.

| Method | Params | `data` |
|--------|--------|--------|
| `talk` | `to` (`user:tty`), `message` (may be empty) | the engine's result: invite sent, invite accepted, or line sent |
| `talk_read` | none | `lines` (the rendered text, one entry per line), `messages` (`kind`, `label`, `body`), `invites` (`from`, `tty`, `accept`), `ended`, `accept_published`, `opening` (the line an accepted invite carried, sent by this read), `opening_unsent`, `dropped` (frames the full queue lost since the last read), `phase`, `peer` |
| `talk_end` | none | `{"ended": bool}`: true when a talk or an outstanding invite was closed |

A notice is `talk_invite`, `talk_accept`, `talk_line` or `talk_end`, each with
`data` `{"from": str, "tty": str, "pending": int}`. Text never travels in an
event: biff pulls bodies and pushes notice. A client that wants the text calls
`talk_read`, which drains the held state, so latency is one round trip after the
notice. A notice is pushed as the frame arrives; mail and wall arrivals are still
found by polling. A client that subscribes with a backlog calls `talk_read` once
to collect it, because notices are not replayed. A relay that carries no talk (the
local relay) answers `talk` and `talk_read` with `Talk requires a NATS relay
connection.`

## Supervision

biffd runs as a user-level service, with no `sudo`:

| Platform | Unit | Notes |
|----------|------|-------|
| macOS | LaunchAgent `com.punt-labs.biffd` | As voxd's LaunchAgent (vox DES-038) |
| Linux | systemd user unit `biffd.service` | As luxd and quarryd; voxd's system-level unit needs `sudo` and is not the pattern |
| No service manager (an agent-image container) | A detached `biffd` process | The client's autostart starts it, as agent-image starts `quarryd` |

Install, start, stop and diagnosis use `punt-service`
(`lux/packages/punt-service`), the shared service library that lux's README
tells daemon authors to adopt. biffd supplies one `ServiceSpec`:

| Field | Value |
|-------|-------|
| `launchd_label` / `systemd_unit` | `com.punt-labs.biffd` / `biffd.service` |
| `binary_name` | `biffd` |
| `cli_verb` / `cli_name` | `daemon` / `biff` |
| `health_port` | 8424, so the library's port guard refuses to install over another listener |
| `restart_on_crash_only` | true: a clean stop by the operator stays stopped |
| `log_path` | `~/.punt-labs/biff/logs/biff.log`: `punt-observe` names the file after the tool, so the daemon writes the log every biff process writes, with its role set to `daemon` |

Commands:

| Command | Effect |
|---------|--------|
| `biff install` | Installs biffd's service as part of the two-phase install, after provisioning the home tree |
| `biff daemon install`, `uninstall` | Install or remove the unit alone |
| `biff daemon start`, `stop`, `restart` | Control the running service |
| `biff daemon status` | Installed, loaded, port, version and uptime. Relay connections and attached clients print as "not reported by biffd yet": biffd has no method that returns them, and `biff doctor` checks the relay in the meantime |
| `biff daemon serve` | Run in the foreground; what the unit and the container autostart execute |

`biff doctor` gains daemon checks, each a `punt-service` doctor result:

1. The unit is installed.
2. The unit is loaded and the process is running.
3. The port is bound by biffd and by nothing else.
4. `/health` answers within one second.
5. The token file exists at mode 0600.
6. The daemon version equals the client version.
7. Each relay connection is up, or names why it is not. Until biffd has a method that returns its relay connections without opening a session (an opened session takes part in mail routing), this line says the relay is not reported and only checks that biffd answers.

Each failing check names the command that repairs it.

**Restart policy.** A crash restarts biffd; a clean stop stays stopped. A
failure that no restart can cure, such as a port that is taken or a run directory
that is not private, exits with status 78 (`EX_CONFIG`) after writing its reason
to `~/.punt-labs/biff/logs/biff.log`. The systemd unit does not restart on 78
and limits any other crash loop to five starts a minute; the LaunchAgent has a
30-second `ThrottleInterval`, because launchd cannot skip a restart by status.
Both units start biffd with an explicit `--port 8424`, so `serve.port` records
a port but never decides where the unit listens.

## When the daemon is absent

A client never hangs and never silently does nothing (R-D.7). The sequence:

1. Read the port from `~/.punt-labs/biff/run/serve.port` (8424 when the file
   is absent, so a `--port` override reaches every client), then connect to
   `127.0.0.1:<port>` with a 1-second timeout. A record left by a crash can name
   a port nothing listens on, so the client also tries 8424, the port the unit
   starts biffd on, before it starts anything.
2. On refusal, nudge: start the unit (`launchctl kickstart`, `systemctl --user
   start`), or start a detached `biffd` where no manager exists, then poll
   `/health` until it answers or 5 seconds pass.
3. On success, continue. On failure, exit with an error that separates the two
   causes, as quarry's client does:

   - Not installed: `biffd is not installed. Run: biff install`
   - Installed but down: `biffd is installed but did not start within 5
     seconds. Run: biff daemon status, then biff doctor`

A hook that cannot reach the daemon within its time limit exits without
blocking the harness and records the failure in
`~/.punt-labs/biff/hook-errors.log`. An MCP tool call returns the same error
text as a tool error, so the model and the user both see the cause.

## Sessions across concurrent clients

Several clients run at once: two Claude Code sessions, an opencode session, a
shell. The daemon applies the session-record lifecycle of
[mcp-caller-identity § 4.2](https://github.com/punt-labs/punt-kit/blob/main/standards/mcp-caller-identity.md)
unchanged:

- A session is keyed by `session_id` and the caller's role, from the client's
  `hello`. A shell (`cli`) routes to different relay keys than an agent-side
  client (`mcp`, `hook`, `plugin`), so a shell and an agent that hold one id
  are two sessions with their own inboxes and presence rows: keyed by the id
  alone, the second would be handed the first's inbox. A second client of the
  same id and role attaches to the existing record; the daemon never holds two
  records for one key, and refuses another user or relay under it.
- A new `main_agent_id` for a known session is a new run: the daemon releases
  the previous run's subscription and pending deliveries, keeps durable state,
  and refuses calls from the replaced run with a named error.
- A dropped connection detaches a client and does not end its session. Only
  SessionEnd, or the retention expiry for a crash, ends it. A detached session
  is dormant until its client reattaches.
- Each connection is bound to the one session its `hello` named. A request
  cannot act on another session's inbox.
- A `biff` command run outside any harness session identifies itself as a CLI
  session by the existing command-line session store (PL-245, PL-246), carried
  in `hello` as client type `cli`.

A second client attaching must not disturb the first: its `hello` neither
changes another session's record nor interrupts another connection's
subscription. This is an acceptance test (see Migration).

### Methods the `biff mcp` client needs

These additions let `biff mcp` carry the in-process tools' behaviour through the
daemon. The in-process MCP text is the oracle for every text a method returns.

| Method | Param or field | Meaning |
|--------|----------------|---------|
| `plan` | `source` (`manual`, the default, or `auto`) | A hook sets `auto`. The engine refuses to replace a non-empty manual plan with an auto one and replies `Plan unchanged (manual):` with `data` `{"plan": <the kept plan>, "unchanged": true}`; the client then rewrites the plan-gate marker, which a new session clears. Bead-id expansion and the marker stay in the client. Any other `source` is refused as `invalid_params`. |
| `status` | `relay_counters` | `{"attempts": int, "timeouts": int}` for a relay that counts its requests (NATS), `null` for the local relay. The counters are the daemon's relay for the session's repository, shared by every session on it, so they count since the relay was opened, not since one client started. The status text is unchanged. |
| `end` | none | Ends the caller's own harness session (`claude-session-end.tex`, `EndOwnSession`). A connection of the current run only: one of a replaced run is refused `session_replaced` and nothing changes; a shell session is refused `invalid_params` and ends with `session_end`. New requests are refused and those running are waited for, then, each idempotent: held and unread mail is forwarded to the user mailbox (a consuming relay's mail is held in the outbox until forwarded, so a relay that drops keeps it), the row is deleted, mail accepted meanwhile is forwarded, the tty name is released by compare-and-delete, a `logout` is appended. The record is dropped only when all are done; a step that fails leaves it and lifts the refusal, and the next `end` finishes. Afterwards every request of the session answers `session_ended`, and a second `end` succeeds and does nothing. |
| `arrivals` | none | The snapshot a client starts from or checks against, read-only (`notification-mail-event.tex`: it marks no mail read): `data` `{mark, unread, wall, invites, talk_pending}`. `mark` is the high-water mark of the session's inbox; `unread` is the relay's count plus the mail the daemon holds; `wall` is the active wall or `null`; `invites` lists `{from, tty}` of pending talk invites and `talk_pending` counts queued talk frames. The `mail` event now carries `{count, mark}`; the mark orders what a client was told. |

**The heartbeat carries the activity stamp.** The PostToolUse hook touches
`<unread_dir>/<session_id>.active` after every tool call, and DES-097 has the
heartbeat pass that time to `relay.heartbeat(key, at)` so `/who` idle follows
every tool call. The daemon's heartbeat (`Heartbeat`, reading `ActivityStamp`) does the same: the
daemon derives the path from the hello's session id and its own unread directory
(`~/.punt-labs/biff/unread`), and never takes a path from a client, so a client
cannot make the daemon read any other file. The session id has passed the id
charset, so it cannot climb out of that directory. A missing stamp means no
activity is claimed; a stamp older than the row's `last_tool_at` moves nothing.

**Retention.** A harness session with no connection for `RETENTION_SECONDS` (the
lifetime of its relay row, three days) is ended by the session sweep with the same
effects (`ExpireClaude`); a hello that reattaches first restarts the clock. A relay
failure leaves the record and the next sweep retries. The daemon keeps harness
records in memory, so a restart forgets them (`claude-session-end.tex` models the
record as durable): rows of sessions that never reattach age out by the relay's own
row lifetime, and the held batch is lost with the outbox (PL-1753).

**Push and poll are one path.** The relay publishes a wake poke when mail or a
wall arrives (a frame to the user's repository subject, the repository's wall
subject, or the recipient session's talk subject). The daemon's `PushFeed`
subscribes to those subjects for each attached session and sets a wake flag in
its `Wakes`; the session's poll (`ArrivalPoll`) waits on that flag or on its
interval, and runs the same pass either way: read the mark and the wall, announce
what rose. A poke repeated changes nothing (a flag, not a count), a poke lost is
covered by the interval, and the pass reads every kind, so a kind that keeps
arriving cannot starve another (`PushWake` and `PollWake` in
`notification-mail-event.tex`). The feed subscribes again when the relay dials a
new client, as the talk feed does.

## Z specification obligations

The daemon adds states the current specs do not have. Per the repository's
formal-methods policy they are modeled before or with the code.

| Spec | Gains |
|------|-------|
| `docs/specs/nats-relay.tex` | Client attached, detached and daemon absent; the relay handle is owned by the daemon and exactly one exists per target; a client has no handle |
| `docs/specs/session-model.tex` | Per-session attachment; detach is not session end; a new run releases the old run's subscription; daemon restart returns every record to dormant until reattach |

Invariants the models must prove:

1. No client state holds a relay handle.
2. A request before a valid `hello` changes nothing.
3. Detach never ends a session; only SessionEnd or retention expiry does.
4. After a daemon restart, every reattached client's unread summary equals the
   durable unread state: no arrival is lost and none is duplicated.
5. Every state with an attached client can reach a state in which it receives
   its pending arrivals (no wedged state; the lesson of DES-016 to DES-029).

The gate for the work is `/z-spec:check`, `/z-spec:test` and zero GAP on
`/z-spec:audit` for both specs.

## Package layout

biffd adds two packages to the DES-088 table:

| Package | Holds | May import |
|---------|-------|------------|
| `daemon` | The listener, the frame handler that maps each `method` to a command object, the relay owner, the session records, the subscription fan-out | `kernel`, `wire`, `engine`, `render`, `commands` |
| `client` | The connection, `hello`, request and reply, reconnect, and the autostart nudge | `kernel`, `wire` |

`server`, `cli` and each `plugins/<tool>` adapter import `client` instead of
`engine`. After migration no client-side package imports `engine`, which is
what makes "no client holds a relay connection" checkable by
`make check-layers`. Process startup in `cli` no longer constructs a relay;
only `daemon` does.

`DeliverySink` becomes the subscription:

- Inside the daemon, each attached subscriber is a sink that writes an `event`
  frame.
- A harness whose delivery handle lives in the harness's own process stays in
  a client: Claude Code's channel sender (in `biff mcp`), the opencode and pi
  plugins.
- A harness whose delivery needs only the session name runs as a sink inside
  the daemon: Codex's queue delivery.

The four rules of DES-088 hold: entry points reach the engine only through
`commands`, arrivals leave through `DeliverySink`, `wire` types stay plain
data (they now cross the connection), and the engine holds no per-tool state.

## Harness adapters

The daemon removes the per-harness need to keep a process alive and polling.
The adapters, tracked as the Codex queue worker, the opencode plugin, the pi
extension and Devin delivery (Linear PL-236, PL-237, PL-238 and PL-1467):

| Harness | Delivery |
|---------|----------|
| Claude Code | `biff mcp` subscribes and emits channel events (DES-087); polling stays only where channels are off |
| Codex | A sink in the daemon runs `codex queue --thread <session-name> --message ...` (or `thread/queue/add`), delivering a visible turn even when the session is idle (`research/harness-compatibility.md`) |
| opencode | The TypeScript plugin subscribes and calls `client.session.prompt` and `tui.showToast`; its tools are the daemon's methods |
| pi | The TypeScript extension subscribes and calls `pi.sendMessage` |
| Devin | Designed with PL-1467; it uses the same subscription |

Every command is a daemon method, so a harness's native tools are generated
from the command list and cannot drift from the MCP tools. The plugin
and the daemon check the protocol `version` in `hello`, which is how a harness
package and the PyPI release stay in lockstep.

Open questions the opencode plugin depends on stay open until measured:
how `session.prompt` behaves against a busy session (SP-1), what
`noReply: true` context costs and whether it survives compaction (SP-2), and a
plugin's restart and crash isolation (SP-4). The client protocol already
assumes a plugin reconnects with `hello` after its host restarts it.

## Migration

The current single-process `biff mcp` keeps working until the daemon path
reaches parity (R-D.8). Steps, each its own pull request:

1. Add `client`, `daemon` and the protocol, with the Z models, serving every
   command. `biff daemon serve` runs; nothing uses it yet.
2. Route `biff watch` and the `biff` command through the daemon.
3. Run the tier 1 to 4 tests twice: against today's in-process path and
   against the daemon path.
4. Route `biff mcp` and the hooks through the daemon behind an opt-in setting.
5. When every tier 1 to 4 test passes on the daemon path, make it the default
   and delete the in-process path in the same pull request. No second path
   remains and no compatibility alias is kept.

Acceptance for parity: the Claude Code plugin and the CLI run entirely through
biffd; every tier 1 to 4 test passes against it; a second concurrent client
attaches without disturbing the first; and the ProB gate shows zero GAP on both
specs.

Adapter tests: each harness plugin has at least one end-to-end test in which a
message sent through the relay reaches a live session of that harness through
the subscription (R-OC.8 for opencode, built on the same fixtures).

## Requirements traceability

These ids came from the retired `requirements-biffd-multihost.md`. The R-A and
R-P ids are covered by [trust-model.md](trust-model.md).

| Id | Covered in | Note |
|----|------------|------|
| R-D.1 | What biffd owns | |
| R-D.2 | What stays in clients; Package layout | Enforced by layers: no client package imports `engine` |
| R-D.3 | Sessions across concurrent clients | |
| R-D.4 | What biffd owns (message semantics) | |
| R-D.5 | Subscription | |
| R-D.6 | Z specification obligations | |
| R-D.7 | When the daemon is absent | |
| R-D.8 | Migration | |
| R-CC.1 | What stays in clients; Migration | `biff mcp` keeps tool descriptions, `tools/list_changed`, the status line and the display pipeline; parity is the acceptance test |
| R-CC.2 | Dropped | Stale: Claude Code channels shipped (DES-087) and the ruling is that channel events are arrival notices and bodies are pulled by a tool call; trust display is in trust-model.md |
| R-CC.3 | Dropped | Done: channels are adopted as a delivery change (DES-087) |
| R-CC.4 | Supervision | biffd ships inside `punt-biff`, so the two release channels are unchanged |
| R-OC.1 | Harness adapters | |
| R-OC.2 | Subscription | |
| R-OC.3 | Harness adapters | Busy-session policy waits on SP-1 |
| R-OC.4 | Harness adapters | Tools are generated from the command list; the `forward` verb is in trust-model.md |
| R-OC.5 | package-structure.md, plugin contract | `Installer` deposits each tool's commands |
| R-OC.6 | package-structure.md, plugin contract | `SessionStart`; the mechanism waits on SP-2 |
| R-OC.7 | Harness adapters | The `hello` version check |
| R-OC.8 | Migration, adapter tests | |
| SP-1 | Harness adapters | Open |
| SP-2 | Harness adapters | Open |
| SP-4 | Harness adapters | Open; the protocol assumes reconnect |
