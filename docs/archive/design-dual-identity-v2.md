# Design: Session Identity Model (v2)

> Second pass — the human is not the anchor. The session is the container.
> Humans and agents are both entities within it.

## Revision Context

v1 assumed a human-centered model: one human per session, agent as annotation.
This fails for:

- **Subagents**: a single session can spawn N subagents in parallel
- **Headless sessions**: `claude -p "deploy"`, Claude SDK — no human at all
- **Agent teams**: multiple top-level agents coordinating
- **The human is not necessary**: they're just another entity, sometimes present

## The Actual Reality

A Claude Code session is a **container**. What's inside it varies:

| Scenario | Humans | Agents | Example |
|----------|:------:|:------:|---------|
| Pair programming | 1 | 1 main + N subagents | Typical Claude Code |
| Headless CI | 0 | 1+ | `claude --dangerously-skip-permissions` |
| Agent teams | 0-1 | N top-level agents | Forthcoming capability |
| CLI only | 1 | 0 | `biff write @eric "hey"` |
| SDK | 0 | 1 | `claude_agent_sdk.query(...)` |

## Design Variant 1: Process Tree Model

Model everything — humans and agents — as **processes** in a session. The BSD
`ps` analog.

### Data Model

```text
Session (the terminal)
├── session_id: str          # unique (replaces current tty)
├── tty_name: str            # human-readable ("tty1", custom name)
├── hostname: str
├── pwd: str
├── created_at: datetime
└── operator: str | None     # who launched it (human login, or None)

Process (anything that can act)
├── pid: str                 # unique within session
├── session_id: str          # which session
├── ppid: str | None         # parent process (None = top-level)
├── kind: "human" | "agent"
├── name: str                # humans: github login; agents: agent type/role
├── display_name: str
├── plan: str | None
├── last_active: datetime
├── status: "running" | "idle" | "stopped"
└── capabilities: set[str]   # "send", "receive", "talk", etc.
```

### The Process Tree in Practice

```text
Session tty1 (operator: kai)
├── PID 1  human   kai         "reviewing eric's PR"     idle 3m
├── PID 2  agent   claude      "running test suite"      active
│   ├── PID 3  agent  explore  "searching for API usage" active
│   └── PID 4  agent  explore  "checked test patterns"   stopped (0)
└── PID 5  agent   claude      (agent team member #2)    active

Session tty2 (operator: None)  ← headless CI
└── PID 1  agent   claude      "deploying v0.9.0"        active
    └── PID 2  agent  test-runner  "smoke tests"          active
```

### Commands

| Command | What It Shows | Analog |
|---------|---------------|--------|
| `who` | Sessions and their top-level entities | BSD `who` |
| `ps` | Process tree within a session (or all) | BSD `ps` |
| `finger @entity` | Deep info on a specific entity | BSD `finger` |
| `write @entity` | Message a specific entity | BSD `write` |
| `wall` | Broadcast to all entities in the repo | BSD `wall` |
| `kill @entity` | Signal an agent to stop | BSD `kill` |

### Addressing

```text
@kai              → the human "kai" (any session)
@kai:tty1         → kai specifically in tty1
@tty1:2           → PID 2 in session tty1 (the main agent)
@tty1:3           → PID 2's subagent (the explore agent)
@tty2             → session tty2 (all top-level entities receive)
```

### Example Output: who

```text
$ /who
SESSION   OPERATOR   ENTITIES          PLAN                    IDLE
tty1      kai        kai, claude(2)    reviewing PR #42        3m
tty2      —          claude            deploying v0.9.0        0m
tty3      eric       eric, claude(4)   refactoring auth        1m
```

### Example Output: ps

```text
$ /ps @kai
PID  PPID  KIND    NAME       STATUS   PLAN                    ACTIVE
1    —     human   kai        idle     reviewing PR #42        3m ago
2    —     agent   claude     running  running test suite      now
3    2     agent   explore    running  searching API usage     now
4    2     agent   explore    done     checked test patterns   1m ago
```

### Trade-offs

**Pros:**

- Uniform model — humans and agents are the same kind of thing
- Process tree naturally models subagent hierarchy
- `ps` is a powerful introspection tool teams will want as agent count grows
- Headless sessions are natural — session with no human process
- Agent teams: multiple top-level agents in one session
- `kill` gives agent lifecycle control through the communication system

**Cons:**

- PIDs are unfamiliar — agents don't have stable names across sessions
- More complex relay storage (sessions + processes vs flat sessions)
- Process lifecycle management (who reaps dead subagents? TTLs?)
- Process metaphor might confuse non-Unix people

## Design Variant 2: Flat Peer Model

Every entity — human or agent — is a **peer** with its own session entry. No
hierarchy. No nesting. The session becomes a group label.

### Data Model

```text
Entity (the only first-class object)
├── id: str                  # globally unique
├── kind: "human" | "agent"
├── name: str                # github login, agent role, etc.
├── display_name: str
├── group: str | None        # ties entities sharing a workspace
├── parent_id: str | None    # for subagents: who spawned me
├── tty_name: str
├── plan: str | None
├── last_active: datetime
├── status: "active" | "idle" | "offline"
```

### Example Output

```text
$ /who
NAME            KIND     GROUP    PLAN                    IDLE
kai             human    ws-1     reviewing PR #42        3m
claude          agent    ws-1     running test suite      0m
claude          agent    ws-1     searching API usage     0m  (subagent)
claude-ci       agent    ws-2     deploying v0.9.0        0m
eric            human    ws-3     refactoring auth        1m
claude          agent    ws-3     writing tests           0m
```

### Addressing

```text
@kai                → kai the human
@claude:ws-1        → the main claude in kai's workspace
@ws-2               → everyone in workspace 2
```

### Trade-offs

**Pros:**

- Dead simple model — one table, one kind of entity
- Subagents are just entities with a `parent_id`
- Headless is natural — a group with only agent entities

**Cons:**

- `/who` becomes noisy fast (5 entities × 3 sessions = 15 rows)
- Group membership is ad-hoc
- Agent names collide — every session has a "claude"

## Design Variant 3: Session + Roster Model

The session is a **container with a roster**. Humans and agents are roster
entries. Closest to the current model but generalized.

### Data Model

```text
Session
├── session_id: str
├── tty_name: str
├── hostname, pwd, created_at
└── roster: list[RosterEntry]

RosterEntry
├── entity_id: str           # unique within session
├── kind: "human" | "agent"
├── name: str
├── display_name: str
├── role: str                # operator, assistant, subagent, team-member
├── parent_id: str | None    # subagent → parent agent
├── plan: str | None
├── last_active: datetime
├── status: "active" | "idle" | "exited"
```

### Roles

| Role | Who | Send? | Receive? | In /who? |
|------|-----|:-----:|:--------:|:--------:|
| `operator` | Human who launched session | yes | yes | yes |
| `assistant` | Main agent | yes | yes | yes |
| `team-member` | Agent team peer | yes | yes | yes |
| `subagent` | Spawned by an agent | yes | configurable | no (/ps only) |
| `observer` | Human watching, not driving | no | yes | yes |

### Example Output: who

```text
$ /who
SESSION   ROSTER                  PLAN                    IDLE
tty1      kai + claude(+2)        reviewing PR #42        0m
tty2      claude                  deploying v0.9.0        0m
tty3      eric + claude(+4)       refactoring auth        1m
```

### Example Output: ps

```text
$ /ps tty1
ENTITY    KIND    ROLE        STATUS    PLAN                    ACTIVE
kai       human   operator    idle      reviewing PR #42        3m ago
claude    agent   assistant   active    running test suite      now
  explore agent   subagent    active    searching API usage     now
  explore agent   subagent    done      checked test patterns   1m ago
```

### Addressing

```text
@kai              → kai across any session
@kai:tty1         → kai specifically in tty1
@tty1             → session tty1 (all roster members receive)
@tty1/claude      → the main agent in tty1
```

### Trade-offs

**Pros:**

- Session remains the primary unit (easier migration from current model)
- Roster captures "who's in this session" naturally
- Roles give structure without rigid hierarchy
- `/who` stays clean (summarized), `/ps` gives depth
- Headless = session with no operator role
- Agent teams = multiple team-member roles

**Cons:**

- Role taxonomy must be right from the start
- Session-scoped entity IDs need session qualifier for cross-session addressing

## Cross-Cutting Design Questions

These need answers regardless of which variant is chosen.

### 1. Agent Naming

Every session has "claude." How to distinguish?

- **Auto-number**: `claude-1`, `claude-2` (like `tty1`, `tty2`)
- **Role-based**: `claude-reviewer`, `claude-deployer`
- **Session-scoped**: always qualified (`@tty1/claude`)

### 2. Subagent Lifecycle

Subagents are ephemeral — they start, work, exit. Questions:

- Register with relay on spawn, deregister on exit?
- Only appear in `/ps`, never in `/who`?
- Have inboxes? Can you write to a subagent?

### 3. Message Routing for Headless Sessions

No human means no status line. Who reads messages?

- Main agent via `/read`?
- Push delivery (agent gets notified without polling)?
- Both?

### 4. Agent Teams Topology

Are agent teams:

- Multiple top-level agents in **one session** (shared workspace)?
- Multiple sessions in a **named group** (separate workspaces, coordinated)?
- Both, depending on configuration?

### 5. Identity Resolution for Agents

Humans get identity from GitHub. What about agents?

- **Inherited**: `kai/claude` (kai's agent)
- **Session-scoped**: `tty1/claude`
- **Self-declared**: agent names itself on startup
- **Headless**: derived from SDK caller or CI context

## Assessment

Variants 1 (Process Tree) and 3 (Session + Roster) are closest. Both share:

- Session as the container
- Entities (human and agent) as guests within it
- `/ps` as the new command for agent-dense visibility
- Hierarchical subagent modeling

The process model is more powerful and conceptually clean. The roster model is
more pragmatic and closer to the current implementation.
