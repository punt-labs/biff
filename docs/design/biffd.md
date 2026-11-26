# biffd: the Per-User Daemon

Status: proposed design (DES-095). Migration step 1 is built: `biff daemon
serve`, the frames and the session records below. Nothing routes through the
daemon yet.

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

A client holds no relay connection and no private key. It sends requests,
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
| `challenge` | client to daemon, first frame | A random 128-bit `nonce`. Nothing secret |
| `proof` | daemon to client | HMAC-SHA256 of the nonce, keyed by the token, as hex |
| `hello` | client to daemon, after the proof checks | Token, client type (`mcp`, `cli`, `hook`, `plugin`), and the caller facts: `session_id`, `main_agent_id` (empty when the harness states none), `cwd` (the directory the client runs in, which names the repository and so the relay target) and an optional `user` override. The daemon answers a valid hello with a `reply` whose `id` is 0 |
| `request` | client to daemon | `id`, `method`, `params`. One `method` per command object in `commands/` |
| `reply` | daemon to client | `id` and either `result` (plain `wire` data) or `error` (a code and an actionable message). A command's `result` is `{"data": <the command's JSON payload>, "error": <bool>}` |
| `subscribe` | client to daemon | Starts the arrival subscription for the connection's session |
| `event` | daemon to client | One arrival summary, with a per-daemon `seq` |

**The daemon proves itself first.** The client's port is a guess (8424 when
no port file exists), and any local process can bind a port biffd is not using,
so the client must not hand the token to whatever answers. The client sends a
`challenge`; the daemon answers with the `proof`; the client compares it with
`hmac.compare_digest` against the HMAC it computes from its own token file, and
sends `hello` only on a match. A missing, late or wrong proof ends the attempt
with an error that names the port and says the token was not sent. The client
also reads the token only from a run directory at mode 0700 and a token file at
mode 0600, both owned by the current user, and refuses otherwise with the
`chmod` that repairs it.

A frame other than a `challenge` first, a `hello` before the proof, a second
`hello`, a request before a valid `hello`, or a wrong token closes the
connection and changes no state. A `hello` whose `version` the daemon does
not support gets an error naming both versions and the command that fixes the
mismatch (`biff daemon restart`, or upgrade the client).

Results are `wire` data and never rendered text. The client runs `render`, so
`biff who` prints the same table whether the command ran in the daemon or in
today's single process.

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
| `log_path` | `~/.punt-labs/biff/logs/biffd.log` |

Commands:

| Command | Effect |
|---------|--------|
| `biff install` | Installs biffd's service as part of the two-phase install, after provisioning the home tree |
| `biff daemon install`, `uninstall` | Install or remove the unit alone |
| `biff daemon start`, `stop`, `restart` | Control the running service |
| `biff daemon status` | Installed, loaded, port, version, relay connections, attached clients |
| `biff daemon serve` | Run in the foreground; what the unit and the container autostart execute |

`biff doctor` gains daemon checks, each a `punt-service` doctor result:

1. The unit is installed.
2. The unit is loaded and the process is running.
3. The port is bound by biffd and by nothing else.
4. `/health` answers within one second.
5. The token file exists at mode 0600.
6. The daemon version equals the client version.
7. Each relay connection is up, or names why it is not.

Each failing check names the command that repairs it.

## When the daemon is absent

A client never hangs and never silently does nothing (R-D.7). The sequence:

1. Read the port from `~/.punt-labs/biff/run/serve.port` (8424 when the file
   is absent, so a `--port` override reaches every client), then connect to
   `127.0.0.1:<port>` with a 1-second timeout.
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

- A session is keyed by `session_id` from the client's `hello`. A second client
  for the same session id attaches to the existing record; the daemon never
  holds two records for one id.
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
| R-CC.2 | Dropped | Stale: Claude Code channels shipped (DES-087) and the ruling is that channel events carry message bodies; trust display is in trust-model.md |
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
