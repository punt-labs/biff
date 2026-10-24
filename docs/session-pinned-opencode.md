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
OpenCode ID, and declare its agent persona before starting Biff. Verify:

```bash
ethos session create --session ses_EXAMPLE --root-id jim --root-persona jim \
  --primary-id ses_EXAMPLE --primary-persona claude
ethos session roster ses_EXAMPLE --json
ethos show claude --json
```

The roster must report `session: ses_EXAMPLE` and exactly one participant
whose `agent_id` is `ses_EXAMPLE` and whose `persona` is the desired agent
handle. The identity JSON must report the same handle, a nonempty name,
and `kind: agent`. Launch the command above from the intended repository
so normal relay configuration and enablement still apply. Do not install
or reconnect live tools as part of implementation validation.
