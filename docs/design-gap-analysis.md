# Gap Analysis: Current System vs Z Model

> Maps the current biff implementation to the Z specification
> (`session-model.tex`), identifies what is and isn't represented, and
> scopes what Variant 1 (Process Tree) addresses.

## Context

Biff is coordination infrastructure for distributed human and agent teams
communicating across machines via NATS. The Z model formalizes what
already exists implicitly across the full distributed topology.

## Step 1: Current System Mapped to BiffWorld

### `orgs` — The Trust Boundary

**Current representation: Implicit in credentials.**

The `.biff` file's `[relay]` section and its credentials (token, NKey
seed, `.creds` file) define who can connect to a shared NATS server.
The `[team]` roster names the members. Together these define the
organization: a set of identities sharing a communication namespace
with mutual trust.

**What matters for coordination:** Orgs are the authorization boundary.
When agent teams span repos, the org determines which agents can
discover and message each other. Two teams on different NATS servers (or
different credential scopes) can't coordinate — that's the org wall.

**Gap:** No first-class org entity. The trust boundary is implicit in
config files. If biff grows to support multiple orgs on a shared relay
(multi-tenancy), this must become explicit.

### `repos` — The Work Context

**Current representation: `BiffConfig.repo_name` — single string, NATS
namespace partition.**

Every NATS subject and KV key is prefixed with `repo_name`. When kai has
Claude Code open in `biff/` and `quarry/`, those are two separate MCP
servers with two separate relay namespaces. `/who` in the biff session
shows biff sessions; `/who` in quarry shows quarry sessions. The repo is
the primary partitioning axis.

**What matters for coordination:** Agents working on related repos need
cross-repo awareness. The langlearn orchestrator needs to coordinate
agents in `langlearn-tts/`, `langlearn-anki/`, and
`langlearn-imagegen/`. An architect agent in `biff/` might need to check
what's happening in `punt-kit/`. Cross-repo coordination requires
knowing which repos exist and who's working in them.

**Gap:** Each session only knows its own repo. There's no cross-repo
discovery. `/who` is repo-scoped with no way to see the broader picture.
The NATS infrastructure could support cross-repo queries (subscribe to
`*.sessions` instead of `biff.sessions`), but the protocol and tools
don't expose it.

### `members` — Identities in the Org

**Current representation: `BiffConfig.team` tuple + identity resolution
chain.**

The team roster is a flat list of GitHub usernames. Identity resolution
(`gh api user` then `getpass.getuser()`) determines the current user.
Any member can message any other member. There's no role, no permission
hierarchy, no distinction between human and agent identities.

**What matters for coordination:** Agent identities don't come from
GitHub. A headless CI agent, an SDK-spawned agent, an agent team
member — these need identities that aren't human GitHub logins. The
member model needs to encompass both human and agent identities. And for
agent teams, you need to know: is this member a human or an agent? What
capabilities does it have? Who spawned it?

**Gap:** Members are assumed to be humans with GitHub accounts. Agent
identities have no registration path. The `team` roster is static (read
from `.biff` at startup) — agents can't dynamically join or leave a
team.

### `machines` — The Physical Topology

**Current representation: `UserSession.hostname` — stored, displayed in
`/finger`, not used for routing.**

Every session records its hostname. When eric runs `/finger @kai`, he
sees `hostname: kais-macbook.local`. But the system doesn't reason about
machines — it doesn't know that kai's macbook has 3 sessions while the
CI runner has 1, or that a particular worktree only exists on a specific
machine.

**What matters for coordination:** Agent teams need machine awareness.
"Spawn an agent to run tests" — on which machine? Where is the worktree?
Can this machine access the repo? If two agents need to share files, are
they on the same machine (fast local path) or different machines (need
relay)? Machine topology determines what coordination strategies are
possible.

**Gap:** Machines are metadata, not entities. No machine registry, no
machine-scoped queries ("show me all sessions on ci-runner-3"), no
topology reasoning.

### `clones` — What's Available Where

**Current representation: Not modeled at all.**

The system doesn't know that kai's macbook has clones of `biff`,
`quarry`, and `punt-kit`, while the CI runner only has `biff`. It
doesn't know that kai has worktrees for `main` and `feature-x` on the
same machine. A session starts in a directory and discovers its repo —
but the broader question of "what repos are available on this machine" is
invisible.

**What matters for coordination:** An orchestrating agent needs to know
where to spawn work. "Run the quarry tests" — which machine has a quarry
clone? "Check out the feature branch" — is there already a worktree, or
do we need to create one? Clone topology is the map of what's possible.

**Gap:** Completely absent. Every session discovers its own context in
isolation.

### `sessions` — The Workspace

**Current representation: `UserSession` — the primary and only
first-class entity.**

This is what biff models well. Sessions have identity (`user:tty`),
presence (`plan`, `last_active`), location (`hostname`, `pwd`), and
liveness (heartbeat, TTL, orphan detection). The relay stores sessions
in KV with auto-expiry. `/who` lists sessions. `/finger` inspects them.
Login/logout events go to wtmp.

**What matters for coordination:** Sessions are where work happens. But
currently a session = a user. With agent teams, a session becomes a
container for multiple workers. A headless session (no human) is an
equally valid workspace. Sessions on different machines working on the
same repo need to coordinate (lock files, branch ownership, merge
conflicts).

**Gaps:**

1. **`soper` is mandatory.** `UserSession.user` has `min_length=1`. No
   headless sessions. SDK invocations, CI agents, and autonomous agent
   teams can't exist in the current model.

2. **No branch tracking.** `UserSession.pwd` is the cwd — a rough
   proxy. Two sessions on `main` and `feature-x` look the same. For
   coordination, branch is critical: "who's working on feature-x?" is a
   fundamental question.

3. **Session = identity.** The session key is `user:tty`. Every message,
   every presence update, every tool call is attributed to the user.
   There's no room for multiple actors within a session.

### `processes` — Who's Actually Working

**Current representation: Not modeled. This is the fundamental gap.**

There is no process concept. The MCP server has one identity
(`state.config.user`), one session key, and every action is attributed
to that identity. When the agent calls `/write`, `Message.from_user` is
the human's name. When 5 subagents run in parallel, they're
invisible — no presence, no identity, no way to address them.

**What matters for coordination:** This is everything for agent teams:

- **Visibility**: `/who` should show that tty1 has kai + claude + 3
  subagents. `/ps` should show the process tree. An orchestrator needs
  to see what agents are running, what they're doing, and whether
  they're stuck.
- **Addressing**: Write to a specific agent. "Hey architect agent, the
  reviewer found a problem." Currently impossible — you can only write
  to the human.
- **Authorship**: When eric gets a message from kai's session, he needs
  to know: was that kai, or kai's agent? Trust and response strategy
  differ.
- **Lifecycle**: Subagents spawn and die. Agent team members join and
  leave. The system needs to track this for presence, for cleanup, and
  for coordination.
- **Hierarchy**: Subagents have parents. Agent team members are peers.
  The process tree captures these relationships — who spawned whom, who
  can signal whom.

**Gap:** The entire layer is missing. No `SpawnProcess`, no
`ReapProcess`, no `pkind`, no `ppar`.

### Summary

| Z Layer | Current State | Needed for Distributed Agent Coordination |
|---|---|---|
| `orgs` | Implicit in credentials | Explicit when multi-tenant or cross-org |
| `repos` | Single-valued, NATS namespace | Cross-repo discovery and coordination |
| `members` | Static human roster | Dynamic membership including agent identities |
| `machines` | Metadata on session | Topology reasoning for spawn placement |
| `clones` | Absent | Spawn planning — where can work run? |
| `sessions` | **Fully modeled** — but human-only, no branch | Optional operator, branch tracking, roster |
| `processes` | **Absent** | The entire agent coordination layer |

## Step 2: What Variant 1 Addresses

Variant 1 (Process Tree) mapped to the Z model's operations.

### Directly Addressed — New Capabilities

#### Process layer (`SpawnProcess` / `SpawnChild` / `ReapProcess`)

The primary contribution. Every session gains a process roster:

- **Claude Code startup**: `SpawnProcess(p1, s, human, "kai")` +
  `SpawnProcess(p2, s, agent, "claude")`
- **SDK/CI startup**: `SpawnProcess(p1, s, agent, "claude")` — no human
  process
- **Subagent launch**: `SpawnChild(p3, p2, agent, "explore")` — child
  of main agent
- **Agent teams**: Multiple `SpawnProcess` calls — peer agents in one
  session, or each in their own session
- **Agent completion**: `ReapProcess` — clean removal, presence
  disappears

In implementation: `ServerState` gains a process registry. The relay
stores processes alongside (or within) sessions in KV. The heartbeat
loop updates process liveness.

#### Headless sessions (`OpenHeadlessSession`)

`soper` becomes partial. A session can exist with no human operator.
`BiffConfig.user` becomes optional or accepts a synthetic identity. The
identity resolution chain (`gh api user` then `getpass.getuser()`)
becomes one path among several — agents get identity from their role,
their SDK caller, or their team configuration.

#### `/ps` command — process visibility

A new tool handler that reads the process roster for a session (or all
sessions). Shows the tree: top-level agents, their subagents, kind,
status, plan, last active. The command that makes agent-dense sessions
legible.

#### Process-aware messaging

`Message` gains process attribution. When the agent sends
`/write @eric`, the message carries enough information for eric to see
it came from kai's agent, not kai. When eric sends
`/write @kai:tty1/claude`, the message routes to the agent process
specifically.

`parse_address` in `tty.py` extends from `@user[:tty]` to
`@user[:tty][/process]`.

### Indirectly Addressed — Existing Capabilities Enhanced

#### `who` output — sessions show their roster

Currently one row per `UserSession`. With processes, each row summarizes
its roster:

```text
SESSION   OPERATOR   ROSTER            REPO    BRANCH      IDLE
tty1      kai        kai, claude(+2)   biff    feature-x   0m
tty2      —          claude            biff    main        0m
tty3      eric       eric, claude(+4)  quarry  main        1m
```

Headless sessions show `—` for operator. The roster count shows agent
density. Requires `sbranch` to become explicit (a `git rev-parse` at
startup).

#### `finger` output — process detail

`/finger @kai` shows kai's sessions AND the processes within them.
`/finger @kai:tty1` shows the full process tree. Extends the existing
`resolve_session` logic to also resolve processes.

#### `plan` — per-process plans

Currently plan is per-session. With processes, each process has its own
plan. The human's plan ("reviewing PR #42") and the agent's plan
("running test suite") coexist.

### Not Addressed — Remains for Later

#### `orgs` — multi-tenancy

Variant 1 doesn't formalize the org layer. Teams are still defined by
shared `.biff` config and relay credentials. Becomes important when biff
coordinates across org boundaries.

#### Cross-repo discovery

Variant 1 operates within a single repo's NATS namespace. Cross-repo
coordination requires either a global namespace or a meta-relay that
bridges repo scopes. The Z model's `repos` and `clones` relations
capture what's needed, but Variant 1 doesn't implement it.

#### Machine topology

`machines` and `clones` remain metadata. Variant 1 doesn't add
machine-level queries or spawn placement logic. When an orchestrator
needs to decide "where should I spawn this agent?", it still can't
answer "which machines have this repo cloned?"

#### Dynamic membership

`members` remains a static roster in `.biff`. Agents can't dynamically
register as team members. The membership model needs to become
dynamic — processes announce themselves, and the org decides whether to
accept them.

### Variant 1 Scope in the Z Model

```text
BiffWorld
├── orgs          ← untouched
├── repos         ← untouched
├── members       ← untouched (agent identity registration is adjacent)
├── machines      ← untouched
├── clones        ← untouched
├── sessions      ← ENHANCED: optional operator, branch tracking
│   ├── smach     ← unchanged (already stored)
│   ├── srepo     ← unchanged (already scoped by NATS)
│   ├── sbranch   ← NEW: explicit branch tracking
│   └── soper     ← CHANGED: partial (headless sessions)
└── processes     ← NEW: entire layer
    ├── psess     ← NEW: process → session mapping
    ├── pkind     ← NEW: human | agent
    ├── pname     ← NEW: process identity
    └── ppar      ← NEW: process tree (subagent hierarchy)
```

Variant 1 adds the bottom layer and refines the session layer. The upper
layers remain implicit. That's the right scoping for the immediate need:
making agents and humans visible, addressable, and distinguishable
within the system that already connects them across machines.
