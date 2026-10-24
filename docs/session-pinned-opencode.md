# Explicit OpenCode session pin (biff-51xp)

## Decision

The initial OpenCode pilot runs one Biff MCP process for one explicitly
named session. This is an opt-in exception to DES-040's Claude discovery,
not generic MCP session multiplexing. Existing process-local descriptions
and subscriptions remain unchanged.

```bash
biff mcp --ethos-session ses_EXAMPLE --ethos-agent-id ses_EXAMPLE
```

Both flags are required together, must match, and must name an existing
Ethos roster participant. Biff reads that exact roster and resolves its
participant's persona with `ethos show`. The identity must be an agent;
the persona's handle and name become Biff's user and display name. No
human, GitHub, OS-user, or repository-default identity fallback is allowed.
`--user` cannot be combined with this mode. Environment variables alone
do not opt in or change the default Claude behavior.

The two read-only Ethos subprocess calls each have a two-second timeout
and receive child-only `ETHOS_SESSION` and `ETHOS_AGENT_ID` values. The
raw session ID is retained for Ethos and caller validation. Routing uses
`SessionHint.derive_routing_id` with an OpenCode-specific domain, and the
unread file is `~/.punt-labs/biff/unread/opencode-<routing-id>.json`.

The process acquires a nonblocking exclusive `flock` on
`~/.punt-labs/biff/session-locks/opencode-<routing-id>.lock` before any
session registration, subscriptions, or unread writes. It holds that lock
through all lifespan teardown, including relay and unread removal. An
overlapping process fails with an explicit "already has a running Biff
process" error, without touching the owner's state. Wait for the predecessor
to exit before reconnecting; the routing ID and inbox remain stable. Crashes
release the kernel lock automatically. Lock files remain on disk deliberately:
unlinking them would allow two owners to lock different inodes.

Both relay-agnostic and LocalRelay sentinel reapers acquire this same routing
lock before reading or deleting a pinned session, retaining it through deletion
and sentinel removal. A live owner makes a foreign reaper skip the key; an
already-running reaper makes replacement startup fail until reaping completes.
Replacement startup consumes its predecessor's sentinels under ownership before
registration. Partial startup failures release acquired aliases, presence,
unread files, active markers, and owned relay connections before unlocking.
LocalRelay discards empty, malformed, or undecodable removal-sentinel artifacts
individually before routing-key locking, so a signal-time interrupted write
cannot block enumeration or unrelated pinned startup. Valid keys still require
ownership through deletion and sentinel removal.
Lifespan cancellation is deferred until its shielded teardown task completes:
even repeated cancellations during logout, task shutdown, or relay close cannot
release ownership while predecessor background tasks are still running. The
original cancellation is then re-raised. Externally owned relays are not closed.
Teardown stages continue after a worker timeout or other stage failure. Workers
are cancelled and joined even on the timeout path; remaining unread, presence,
active-marker, and owned-relay cleanup completes before the first failure is
propagated. Cleanup failures do not replace an original startup/body exception.

**Safe scope:** all processes using a pin must share the same Biff home and
a filesystem providing coherent `flock` semantics. The lock key deliberately
omits persona, repository, and relay because the unread key omits them too.
This is not a distributed lease. Separate homes, containers with independent
home mounts, or hosts with independent filesystems do not exclude one another.
NATS presence uses `<repo>.<user>.<routing-id>` in the relay's KV bucket;
identical pins with the same repo and persona can therefore collide across
hosts even though their unread files are separate. Do not run that combination
concurrently outside the shared lock scope. Cross-host ownership/fencing is
not supported by this pilot; use distinct OpenCode session IDs instead.

Pinned MCP plans update relay presence only, never Claude's plan-gate markers.
They neither create nor clear the shared or ancestor-Claude marker, even if
launched beneath Claude. Unpinned MCP and CLI plans still resolve the Claude
identity fresh on each call.

Every `tools/call` must contain `_meta` with
`"ai.opencode/sessionID": "ses_EXAMPLE"`. Missing, malformed, or different
IDs fail before tool execution. A process pinned to one session cannot
serve another session or a subagent session with a different ID. Configure
a separate process for each participant; a shared client configuration
that sends several session IDs to this process will receive errors.

Automatic human companion registration is explicitly disabled, including
heartbeat retries. Ethos root authority does not demonstrate a human's
live presence. Roster creation and persona declaration must happen before
starting this process; Biff neither creates participants nor changes shared
identity defaults. Native Ethos MCP metadata resolution is independent.

## Pilot prerequisites

Use an Ethos CLI supporting explicit roster reads and
`ETHOS_SESSION` / `ETHOS_AGENT_ID`. Create the pilot roster explicitly
with its existing session-create command, seat the participant under the
OpenCode ID, and declare its agent persona before starting Biff.

**Check the roster first. Run `session create` only if the session is absent:**
it overwrites an existing roster. If the session already exists, inspect it
and use explicit participant operations instead of recreating it.

```bash
ethos session roster ses_EXAMPLE --json
```

For an absent pilot session, create and verify:

```bash
ethos session create --session ses_EXAMPLE \
  --root-id jfreeman --root-persona jfreeman \
  --primary-id ses_EXAMPLE --primary-persona opencode
ethos session roster ses_EXAMPLE --json
ethos show opencode --json
```

The roster must report `session: ses_EXAMPLE` and exactly one participant
whose `agent_id` is `ses_EXAMPLE` and whose `persona` is the desired agent
handle. The identity JSON must report the same handle, a nonempty name,
and `kind: agent`. Launch the command above from the intended repository
so normal relay configuration and enablement still apply. Do not install
or reconnect live tools as part of implementation validation.
