# The Agentic Engineering Landscape: Problems, Players, and Where Biff Fits

**Date:** 2026-02-21
**Status:** Living document — update as landscape evolves
**Related:** [Agent coordination landscape research (2026-02-17)](research-2026-02-17-agent-coordination-landscape.md), [biff-yk1](../.beads/) (prfaq update bead)

---

## The Six Problems

When engineering teams adopt AI agents, six distinct problems emerge. Each operates at a different layer, on a different timescale, and requires different primitives. No single tool solves all six. The landscape is forming around this structure — often without the players realizing they're building adjacent layers, not competing products.

### 1. Intent & Trust — "Why was this built? Is the reasoning sound?"

**The problem:** Agents produce code fast. Reviewers can't keep up. When an agent generates 300 lines from a one-line prompt, the reviewer has no access to the reasoning chain, constraints considered, or alternatives rejected. Zero-trust policies require human sign-off, but the human becomes the bottleneck. Trust collapses because the artifact (code) is decoupled from the intent (why this code, why this way).

**Timescale:** Per-commit. Backward-looking — audit what happened.

**Who's building:**

- **[Entire.io](https://entire.io)** (Dohmke, $60M seed, Feb 2026) — Checkpoints CLI logs prompts, reasoning steps, decisions, and constraints as structured data linked to Git commits. Shadow Branches keep AI context out of main branch. Dohmke: *"a shift from engineering as craft to engineering as specification and intent."* Reviewer clicks a Checkpoint ID, sees the prompt, reads the agent's plan. "Black Box Flight Recorder" for code.
  - Architecture: git-compatible database + universal semantic reasoning layer + AI-native UI
  - Integrations: Claude Code, Gemini CLI, Open Codex (coming)
  - Funding: $60M seed at $300M valuation (Felicis, Madrona, M12)

**What biff doesn't do here:** Biff is not an audit trail. Biff shows who is working *right now*, not why code was written *last week*. These are complementary — a team uses Entire to trust the output and biff to coordinate the people producing it.

---

### 2. Context & Memory — "What was decided and why? Start from shared knowledge."

**The problem:** Agent sessions are isolated. Each new session starts from zero. Prior architectural decisions, team conventions, and design rationale are forgotten. For a solo dev, this is friction. For a team, it's architectural entropy — agents on the same team contradict each other because they don't share memory.

**Timescale:** Cross-session. Forward-looking — inform the next session.

**Who's building:**

- **[SageOx](https://sageox.ai)** (credits Yegge, launched Feb 2026) — "The hivemind for agentic engineering." Four components:
  1. **Team Context** — principles, norms, conventions as grounded inputs
  2. **Ledger of Work** — intent from meetings and sessions becomes queryable context
  3. **Ox CLI** — auto-primes every new agent session with relevant team context
  4. **Web App** — organization, repos, ledger access

  Key quote: *"Agent sessions don't start from zero. They start from shared memory."*

  No real-time communication. No presence. No messaging. Async capture-and-consult. Humans and agents share *memory*, not *conversation*.

**SageOx vs. Entire.io:** Close neighbors, different granularity. SageOx captures team-level decisions to prime future sessions (forward-looking). Entire captures commit-level reasoning to build trust in past output (backward-looking). A team could use both.

**What biff doesn't do here:** Biff is not a context store. `/plan` is a one-line status, not a decision log. Biff shows what someone is doing *now*, not what the team decided *last month*.

**Opportunity for biff:** SageOx's Ledger captures meetings and sessions. Biff conversations (especially `/wall` broadcasts and `/write` exchanges) are exactly the kind of decision-making context that a Ledger should capture. A SageOx integration could automatically feed biff communication into the team's shared memory. This is a *complement* play, not a feature biff builds.

---

### 3. Communication & Presence — "Who is here right now? Talk to them."

**The problem:** Engineers and agents work in the same repo but can't see each other. There's no way to ask "who is active on this project?" and get an answer that includes both humans and agents. There's no way to send a directed message to a specific person or agent without leaving the terminal. Slack shows everything from every project; the signal-to-noise ratio makes it useless for focused engineering communication.

**Timescale:** Real-time. Present — who is here, what are they doing, reach them.

**Who's building:**

- **[Biff](https://github.com/punt-labs/biff)** — MCP-native slash commands for team communication inside Claude Code sessions (and any MCP-compatible client). BSD Unix vocabulary: `/who`, `/finger`, `/plan`, `/write`, `/read`, `/tty`, `/mesg`, `/wall`. NATS relay for cross-machine messaging. `.biff` file scopes communication to the current repo.
  - Identity model: persistent (user identity in `.biff`, survives sessions)
  - Participant model: hybrid (humans and agents are co-equal, both have presence, plans, mailboxes)
  - Scope: repo-scoped, cross-machine, any MCP client
  - 7 shipped commands, `/wall` next

**Nobody else is building this.** This is the core finding of the [agent coordination landscape research](research-2026-02-17-agent-coordination-landscape.md). Every pure-agent framework (CrewAI, LangGraph, A2A, Swarms, claude-flow) treats humans as external operators. Entire and SageOx address trust and memory, not real-time presence and messaging. Agent Teams (below) has a mailbox but it's ephemeral, single-session, and agent-only.

---

### 4. Work Tracking — "What needs doing? What blocks what? Who claimed what?"

**The problem:** As agents and humans produce work faster, the bottleneck shifts from execution to *discovery and sequencing*. What work exists? What depends on what? Who is working on what? What's blocked? These questions span sessions, span days, and require persistent state that survives restarts.

**Timescale:** Multi-session. Strategic — the shape of work over days and weeks.

**Who's building:**

- **[Beads](https://github.com/punt-labs/punt-kit)** (`bd`) — Git-native issue tracking with dependencies, priorities, and status. Issues travel with the repo (`.beads/` directory). Sync via git remote. Commands: `bd ready` (what's available?), `bd blocked` (what's stuck?), `bd dep add` (sequencing), `bd stats` (health).

  Beads is not a project management tool (no sprints, no boards, no epics). It's a *work discovery* tool — it answers "what should I work on next?" for both humans and agents, accounting for dependencies and priority.

- **Claude Code Agent Teams task list** (see below) — session-scoped, ephemeral task tracking within a single team. Does not persist across sessions. Does not track cross-repo or multi-day work.

- **Linear, GitHub Issues, Jira** — traditional issue trackers. Not agent-aware. No dependency-driven work discovery. No git-native storage.

**Beads vs. Agent Teams tasks:** Agent Teams' shared task list is for *within-session coordination* — how do 4 agents divide a single feature? Beads is for *cross-session strategy* — across 50 open issues, which 3 should I work on today? Different timescales, different use cases.

**Opportunity for biff:** `/plan` currently sets a one-line status. If biff could pull the current beads status — e.g., `bd show` for the issue you're working on — `/finger` output could show not just "what are they doing?" but "what bead are they driving?" This bridges communication (biff) and work tracking (beads) without biff becoming a project management tool.

---

### 5. Session Coordination — "How do agents divide work within a task?"

**The problem:** A single task (refactor auth, review a PR, debug a race condition) may benefit from multiple agents working in parallel. They need to divide files, share findings, avoid conflicts, and converge on a result. This is intra-task parallelism, not cross-task strategy.

**Timescale:** Within a session. Tactical — how do we split this work right now?

**Who's building:**

- **[Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams)** (Anthropic, experimental) — One lead + N teammates. Shared task list (pending/in-progress/completed with dependencies). Mailbox for inter-agent messaging. Broadcast capability. Two display modes (in-process, split panes via tmux/iTerm2). Hooks: `TeammateIdle`, `TaskCompleted`.
  - Scope: single machine, single session, single repo
  - Identity: ephemeral (no session resume, no persistent identity)
  - Human model: human is the operator of the lead, not a participant in the team
  - Protocol: Claude Code proprietary (not MCP)
  - Token cost: high (each teammate is a separate Claude instance)

- **Subagents** (Claude Code built-in) — lightweight, report-back-only workers within a single session. No inter-agent communication. Lower overhead than Agent Teams.

- **claude-flow** (ruvnet, MCP-based) — 171 MCP tools, hive-mind coordination with queen-led topology. Humans are supervisors, not participants. Positioned as "production-oriented middleware for augmenting Claude Code."

**Agent Teams vs. biff:** Agent Teams has a Mailbox (messaging) and teammate visibility (presence-ish). But it's single-machine, single-session, ephemeral, and agent-only. Biff is cross-machine, persistent, and hybrid. They operate at different scopes:

| Dimension | Agent Teams | Biff |
|---|---|---|
| Scope | Single session, single machine | Cross-machine, cross-session |
| Identity | Ephemeral (no resume) | Persistent (.biff, NATS identity) |
| Participants | Agents only; human is operator | Humans + agents as peers |
| Protocol | Claude Code proprietary | MCP (any client) |
| Communication | Mailbox (message, broadcast) | /write, /read, /wall |
| Presence | Lead sees teammates | /who, /finger (7-column, enriched) |
| Task coordination | Shared task list | Not in scope (beads handles this) |

**Opportunity for biff:** If Agent Teams teammates can load MCP servers (they do — they read the project's `.mcp.json`), they could use biff to communicate *beyond* their session. A teammate doing code review could `/write kai "found a security issue in auth — need your input"` to reach a human on another machine. Biff becomes the cross-session, cross-machine bridge that extends Agent Teams' reach.

Concretely: a biff plugin hook on `TeammateIdle` or `TaskCompleted` could automatically broadcast status to the biff presence layer. Agent Teams teammates would appear in `/who` alongside human sessions. This makes the hybrid model concrete — you run `/who` and see both your human colleague and the 3 Agent Teams workers on her machine.

---

### 6. Agent Interoperability — "How do opaque agents interoperate?"

**The problem:** Different organizations build agents using different frameworks, different models, different protocols. How does Agent A (built on CrewAI, running GPT-4) collaborate with Agent B (built on LangGraph, running Claude)? This is the RPC/protocol layer — making agents callable across boundaries.

**Timescale:** Infrastructure. Persistent — standards that outlive individual sessions.

**Who's building:**

- **[Google A2A](https://a2a-protocol.org/)** — Agent2Agent Protocol. JSON-RPC 2.0 over HTTPS. Agent Cards at `/.well-known/agent.json` for discovery. 150+ organizations. Linux Foundation. v0.3 adds gRPC. Explicitly preserves agent opacity — agents collaborate *without sharing internal memory*. **No human participant model whatsoever.** This is the clearest evidence that the industry is building pure agent-to-agent infrastructure.

- **[CrewAI](https://github.com/crewAIInc/crewAI)** — Role-based, hierarchical. 100K+ certified devs. Mandatory LLM deliberation per interaction. Human = external operator.

- **[LangGraph](https://www.langchain.com/langgraph)** — Graph-based, immutable shared state. Human = interrupt node. No presence, no mailbox.

- **[Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/)** — Merges AutoGen + Semantic Kernel. Graph-based workflows, native MCP/A2A/OpenAPI. Human = governance gate. GA targeted Q1 2026.

- **[Swarms](https://github.com/kyegomez/swarms)** (kyegomez) — Enterprise multi-agent. Sequential, parallel, DAG, mesh, federated architectures. Human = external operator.

- **[HumanLayer](https://github.com/humanlayer/humanlayer)** — `@require_approval()` decorator. Routes approvals via Slack/email. Human = approval gate, not co-participant.

**What biff doesn't do here:** Biff is not an agent orchestration framework. Biff doesn't route tasks to agents, manage agent lifecycles, or handle inter-agent RPC. Biff is the communication layer that humans use to stay aware of and interact with agents, regardless of which orchestration framework those agents run on.

---

### 7. Remote Execution — "Where is the work running? What's happening in the cloud?"

**The problem:** Engineers dispatch work to cloud VMs and walk away. A `& Fix the auth bug` sends a Claude session to Anthropic's infrastructure where it runs autonomously — cloning the repo, writing code, running tests, pushing a branch. Nobody on the team can see this work happening. It's a ghost worker: productive but invisible. When the engineer closes their laptop, the cloud session continues but the team loses all awareness that work is in flight.

**Timescale:** Asynchronous. Hours — tasks dispatched, run in background, completed later.

**Who's building:**

- **[Claude Code on the Web / Teleport](https://code.claude.com/docs/en/claude-code-on-the-web)** (Anthropic, research preview) — Two-directional session movement:
  - **Terminal → Web** (`& prefix` or `--remote`): Dispatches a task to a cloud VM. Repo cloned, environment set up, Claude runs autonomously. Multiple `&` tasks run in parallel. Session persists even if laptop closes.
  - **Web → Terminal** (`/teleport` or `/tp`): Pulls a cloud session down to local terminal. Checks out the branch, loads full conversation history. One-way — can't push terminal sessions up (& creates a new session instead).
  - **Session sharing**: Team visibility (Enterprise/Teams) or public visibility (Pro/Max). Shareable links. Recipients see session state but not real-time updates.
  - **Requirements**: Same account, same repo, clean git state, branch pushed to remote.
  - Architecture: Isolated Anthropic-managed VMs, security proxy, GitHub proxy for credentials, SessionStart hooks for environment setup.

- **GitHub Codespaces / Copilot Workspace** — Cloud development environments. Not session-based teleportation, but cloud-hosted compute for development.

- **Gitpod, Coder, DevPod** — Cloud development environments with varying degrees of AI integration. Not relevant to the session mobility problem specifically.

**What biff doesn't do here:** Biff is not a compute orchestrator. Biff doesn't dispatch work to VMs or manage cloud sessions. But biff is the **visibility layer** for work running anywhere — and "anywhere" now includes cloud VMs that nobody can see.

**The invisible work problem:** Teleport creates a new category of work that is *productive but invisible*. Before teleport, all work happened in a terminal someone was sitting at. Agent Teams expanded this to multiple local agents, but still on one machine. Teleport expands it further: work runs on a VM in Anthropic's cloud, potentially while the engineer's laptop is closed. The team has zero visibility into this work unless biff (or something like it) makes it visible.

**Opportunity for biff:** This is the strongest integration case. See the Teleport bridge section below.

---

## The Layer Cake

| # | Layer | Question | Timescale | Tool |
|---|---|---|---|---|
| 1 | **Intent & Trust** | Why was this built? Is it trustworthy? | Per-commit | Entire.io |
| 2 | **Context & Memory** | What was decided? Start from shared knowledge. | Cross-session | SageOx |
| 3 | **Communication & Presence** | Who is here? Talk to them. | Real-time | **Biff** |
| 4 | **Work Tracking** | What needs doing? What's blocked? | Multi-session | Beads |
| 5 | **Session Coordination** | How do agents divide this task? | Within-session | Agent Teams |
| 6 | **Remote Execution** | Where is work running? What's in the cloud? | Asynchronous | Teleport |
| 7 | **Agent Interop** | How do opaque agents interoperate? | Infrastructure | A2A, CrewAI, LangGraph |

**Key insight:** No tool spans more than one layer well. Entire doesn't do presence. SageOx doesn't do messaging. Agent Teams doesn't persist. Teleport doesn't do visibility. Biff doesn't do task coordination. This is healthy specialization, not fragmentation — the layers have different timescales, different state models, and different user models.

---

## Where Biff Adds Value

### Unique position

Biff is the only tool in this landscape where you can:

1. Ask "who is working on this repo right now?" and get an answer that includes both humans and agents
2. Send a directed message to any of them
3. See their status, plan, and availability
4. Do all of this from the terminal, in any MCP-compatible client, scoped to the current repo

### What biff is not

- Not an audit trail (Entire)
- Not a context store (SageOx)
- Not a task tracker (beads, Agent Teams tasks)
- Not an agent orchestrator (CrewAI, LangGraph, Agent Teams)
- Not an agent-to-agent protocol (A2A)

### The "only biff can do this" test

A scenario that requires biff and cannot be solved by any other tool in the landscape:

> **9:14 AM.** Kai is at his laptop working on auth. He has 2 Agent Teams workers running locally. He also dispatched a migration task to the cloud 20 minutes ago with `& Migrate the session store from Redis to Postgres`. Eric is on another laptop working on payments with 1 local agent. Eric closed his laptop 10 minutes ago to grab coffee, but his cloud task (`& Fix the flaky payment webhook test`) is still running. The team has SageOx and Entire configured.
>
> Maya joins the project for the first time. She runs `/who`:
>
> ```text
> NAME          TTY    IDLE  S  HOST           DIR                PLAN
> kai           tty1   2m    ✓  kais-mbp       src/auth           [biff-yk1] fixing token refresh
> kai:w1        tty2   0s    ✓  kais-mbp       src/auth/tests     writing auth tests
> kai:w2        tty3   0s    ✓  kais-mbp       src/auth           refactoring middleware
> kai:remote    —      0s    ✓  claude.ai      src/db             migrating session store
> eric          tty1   10m   ✗  erics-mbp      src/payments       payment webhook handler
> eric:w1       tty2   0s    ✓  erics-mbp      src/payments       debugging webhook retry
> eric:remote   —      0s    ✓  claude.ai      tests/payments     fixing flaky webhook test
> @ox           —      0s    ✓  sageox.ai      .                  recording team context
> @entire       —      0s    ✓  entire.io      .                  auditing commits
> ```
>
> **In one glance, Maya knows:**
>
> - **Kai** is at his desk (idle 2m), driving bead biff-yk1, with two local agents on auth and a cloud session migrating the DB
> - **Eric** is away (idle 10m, status ✗) but has two workers still active — one local agent debugging webhooks, one cloud session fixing a flaky test
> - **Nobody** is touching the API layer — she can pick that up
> - **@ox** is capturing team context (decisions won't be lost)
> - **@entire** is auditing commits (work will be traceable)
>
> She runs `/finger eric`:
>
> ```text
> Login: eric                            Name: Eric Chen
> Host: erics-mbp                        Dir: src/payments
> Plan: payment webhook handler
> Sessions:
>   tty1  10m idle  src/payments          (laptop closed)
>   tty2  0s        src/payments          eric:w1 — debugging webhook retry
>   ☁️     0s        tests/payments        eric:remote — fixing flaky webhook test
> Last write: 22m ago
> ```
>
> She runs `/write eric "I'm picking up API rate limiting. Your cloud task on the flaky test — want me to review the branch when it lands?"` — the message goes to Eric's inbox. When he opens his laptop, biff delivers it.
>
> **Twenty minutes later,** Eric's cloud task completes. Biff delivers:
>
> ```text
> @remote → eric: Task "Fix flaky webhook test" completed.
>   Branch: fix/flaky-webhook-test (3 files, +47 -12)
>   /tp fix/flaky-webhook-test to resume locally.
> ```
>
> Eric reads Maya's message, reviews the branch, and `/write`s back: *"Looks good. Branch is clean — merge when ready. I'll review your rate limiting PR after lunch."*

No combination of Entire + SageOx + Agent Teams + beads + A2A + Teleport produces this outcome alone. Each tool does its layer well. Biff is the connective tissue that makes them all *visible to the team*.

And critically: every tool in `/who` — @ox, @entire, the remote sessions — *gains* value from being visible. Transparency of infrastructure is itself a feature.

---

## Integration Opportunities

Ideas for how biff can draw from what others are building, without becoming those things.

### Teleport bridge — visibility for cloud work (high value, medium effort)

**What:** When an engineer dispatches work with `&` (terminal → web), a biff SessionStart hook on the cloud VM registers the remote session in `/who`. The team sees `user:remote` entries for cloud-dispatched work. When the cloud session completes, a hook delivers a completion notification via `/write` and updates presence.

**Concrete features:**

- Cloud sessions appear in `/who` as `user:remote` with HOST `claude.ai` and their working directory
- `/finger user:remote` shows: task description, elapsed time, branch name, file change count
- On completion: `/write` delivers a summary to the dispatching user — branch name, files changed, test results, `/tp` command to resume
- On completion: presence entry transitions from active to "completed (branch: fix/xyz)" before timing out
- If the user closes their laptop (local session drops from `/who`), their remote sessions stay visible — the team knows work is still in flight
- Optional: `/wall` broadcast for significant completions ("kai:remote landed fix/session-store — 12 files, all tests green")

**How it works technically:**

1. Engineer runs `& Fix the auth bug` — Claude Code creates a cloud session
2. Cloud VM boots, clones repo, runs SessionStart hook
3. SessionStart hook: `biff plan "fixing auth bug"` registers presence via NATS relay
4. Cloud session works autonomously — presence stays active in `/who`
5. On completion: hook runs `biff write $USER "Task completed. Branch: fix/auth-bug. /tp to resume."`
6. Presence times out or transitions to completed state

**Why it matters:** Teleport creates *invisible work*. Cloud sessions are productive but nobody can see them. Before teleport, all work happened in a terminal someone was sitting at. After teleport, work runs on VMs in Anthropic's cloud while laptops are closed. Biff makes this invisible work visible. The team sees the full picture: who's at their desk, who has agents running locally, who has work running in the cloud, and which infrastructure services are active.

**The three-layer visibility model:**

| Where work runs | Who sees it today | Who sees it with biff |
|---|---|---|
| Your terminal | Just you | Everyone via `/who` |
| Agent Teams (local) | Just the lead | Everyone via `/who` (Agent Teams bridge) |
| Cloud via teleport | Nobody until you `/tp` back | Everyone via `/who` (Teleport bridge) |

### Agent Teams bridge (high value, medium effort)

**What:** Agent Teams teammates load MCP servers from the project. If biff is in `.mcp.json`, teammates get `/write`, `/who`, `/finger` automatically. Biff becomes the cross-machine bridge that extends Agent Teams beyond a single session.

**Concrete features:**

- Agent Teams workers appear in `/who` as `user:worker-name` (e.g., `kai:w1`)
- Workers can `/write` to humans on other machines
- `TeammateIdle` hook triggers a biff presence update (goes idle in `/who`)
- `TaskCompleted` hook optionally broadcasts to `/wall`

**Why it matters:** Makes the hybrid model tangible. `/who` shows both humans and their agents, across machines, in one view.

### Beads integration into /plan (medium value, low effort)

**What:** When a user runs `bd update <id> --status=in_progress`, their biff `/plan` auto-updates to include the bead title. `/finger` shows the bead ID alongside the plan text.

**Concrete features:**

- `bd update biff-yk1 --status=in_progress` → `/plan` becomes "biff-yk1: Update prfaq competitive positioning"
- `/finger kai` shows: `Plan: [biff-yk1] Update prfaq competitive positioning`
- Clicking/copying the bead ID lets teammates run `bd show biff-yk1` for full context

**Why it matters:** Bridges communication (what am I doing?) and work tracking (what issue am I driving?) without biff becoming a task tracker. The bead is the source of truth; biff is the display layer.

### SageOx Ledger feed (low value now, high value at scale)

**What:** Biff `/write` and `/wall` messages contain decision-making context that SageOx's Ledger could capture. An integration would feed biff communication into the team's shared memory.

**Concrete features:**

- `/wall` broadcasts (team announcements, status changes) auto-captured as Ledger entries
- `/write` exchanges that contain decisions (detected by keyword or explicit `/decide` flag) fed to SageOx

**Why it matters:** Communication is ephemeral; decisions should be persistent. SageOx captures intent; biff carries intent in real-time. The two together create a pipeline: real-time communication → persistent team memory.

### Entire.io Checkpoint awareness (low value, low effort)

**What:** When a teammate commits code with an Entire Checkpoint ID, biff could surface that in `/finger` output. "Last commit: abc1234 (Checkpoint: cp-789)".

**Why it matters:** Lightweight — just display. Connects the communication layer (who is working) to the trust layer (is their output auditable).

### Tool presence — personify infrastructure (high value, low effort)

**What:** Infrastructure tools register as participants in `/who`. Not as humans or agents, but as a third category: *services*. Just like seeing "Otter.ai" in a Zoom meeting tells you the call is being transcribed, seeing `@ox` in `/who` tells you team context is being captured. Seeing `@entire` tells you commits are being audited.

**Concrete features:**

- Tools register presence via biff's MCP interface with a `@` prefix convention (e.g., `@ox`, `@entire`, `@ci`)
- `/who` displays them with a distinct marker (no TTY, no idle — they're always-on services)
- `/finger @ox` returns service-specific info: "Recording team context. Ledger has 47 entries. Last capture: 3m ago."
- `/finger @entire` returns: "Auditing commits. 12 checkpoints today. Last: abc1234 (2m ago)."
- Tools can `/wall` when significant events happen: `@entire` broadcasts "Checkpoint cp-789: auth module refactor (kai, 3 files, 142 lines)"
- `/mesg off` still works — you can mute tool broadcasts just like human messages

**Why it matters:**

1. **Transparency.** Everyone knows what infrastructure is active. No hidden recording. The Zoom analogy is exact — visibility of tooling is itself a trust mechanism.
2. **Discoverability.** New team members see the full picture: who's here, what tools are running, what's being captured.
3. **Composability.** Biff becomes the presence layer for the entire stack, not just for humans and agents. Each tool in the layer cake can register itself without biff knowing anything about what that tool does.
4. **Network effects.** Every tool that registers presence in biff makes `/who` more valuable. Biff becomes the "team dashboard" by default, not by building dashboards.

**Design consideration:** Tool presence should be opt-in and configurable per repo (in `.biff`). Not every project wants `@entire` broadcasting checkpoint summaries. The team decides which tools get presence, just like they decide which humans get `/mesg on`.

---

## Architectural Incompatibilities

Why pure-agent tools cannot serve hybrid teams — not a gap that will be closed incidentally, but a fundamental design tension.

| Tool | Design choice | Why it breaks for humans |
|---|---|---|
| CrewAI | Mandatory LLM deliberation per interaction | Humans can't "check in" without triggering a reasoning loop |
| LangGraph | Human = interrupt node in a graph | No continuous presence; no inbox; no availability toggle |
| OpenAI Swarm/SDK | Intentionally stateless | No persistent identity; no mailbox; no plan |
| A2A Protocol | Agents are opaque services with Agent Cards | Humans have no Agent Card, no presence state |
| Swarms | Mesh/concurrent for sub-second handoffs | Humans can't participate at agent-response latency |
| claude-flow | Humans are supervisors via queen topology | Humans set goals and observe, not `/write` and `/plan` |
| Agent Teams | Ephemeral, single-session, agent-only | No cross-machine reach, no persistence, no human peers |

**Core tension:** All pure-agent tools optimize for throughput (parallelism, minimal latency, no idle state). Human engineers require the opposite: async by default, explicit availability signals, low-frequency but high-intent communication, ability to be "off" without breaking coordination.

---

## Market Signals

- **Gartner:** 1,445% surge in multi-agent system inquiries Q1 2024 → Q2 2025. Predicts 40% of enterprise apps embed AI agents by end of 2026 (up from <5% in 2025).
- **Capgemini:** "By 2028, AI agents will act as a team member with human teams within 38% of organizations."
- **GitHub Copilot:** 20M cumulative signups by mid-2025. 4.7M paid subscribers by late 2025 (3.6x growth in under 2 years).
- **Entire.io:** $60M seed at $300M valuation — largest dev tools seed ever. Signal: the market believes agentic engineering infrastructure is a massive category.
- **SageOx:** Launched same week, credits Yegge. Signal: hybrid team context is being recognized as a distinct problem.
- **Hopf et al. (2025):** Academic research confirms "most studies focus on a single, fixed team configuration" — dynamic co-presence of humans and agents in shared workspaces is understudied.

---

## Open Questions

1. **Will Agent Teams go cross-machine?** If Anthropic adds NATS-like relay and persistent identity to Agent Teams, the overlap with biff grows. Currently experimental with known limitations (no resume, no persistence). Monitor closely.

2. **Will SageOx add real-time communication?** Their Ledger captures async artifacts. If they add a real-time layer (chat, presence), they enter biff's space. Currently no signal of this — their design is explicitly async.

3. **Does Entire's reasoning layer subsume SageOx's context layer?** Both capture "why." At different granularities (commit vs. team), but the boundary could blur. Not biff's problem either way.

4. **Is the "anti-Slack" niche large enough?** All of biff's differentiation assumes engineers want repo-scoped, terminal-native, focus-first communication. If Slack adds an MCP integration or a "focus mode" that's good enough, biff's wedge narrows.

5. **How do agents self-identify in biff?** Today biff identity is human-centric (username from git config). Agent Teams workers need a naming convention that's visible and useful in `/who` output. This is a design decision for the Agent Teams bridge.

---

## Sources

- [Entire.io](https://entire.io) | [TechCrunch coverage](https://techcrunch.com/2026/02/10/former-github-ceo-raises-record-60m-dev-tool-seed-round-at-300m-valuation/) | [The New Stack interview](https://thenewstack.io/thomas-dohmke-interview-entire/) | [SiliconANGLE](https://siliconangle.com/2026/02/10/entire-launches-60m-build-ai-focused-code-management-platform/)
- [SageOx](https://sageox.ai) | [Introducing SageOx](https://sageox.ai/blog/introducing-sageox)
- [Claude Code Agent Teams docs](https://code.claude.com/docs/en/agent-teams)
- [Google A2A Protocol](https://a2a-protocol.org/) | [Announcement](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/)
- [CrewAI](https://github.com/crewAIInc/crewAI) | [LangGraph](https://www.langchain.com/langgraph) | [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/)
- [HumanLayer](https://github.com/humanlayer/humanlayer) | [claude-flow](https://github.com/ruvnet/claude-flow) | [Swarms](https://github.com/kyegomez/swarms)
- [Hopf et al. 2025 — "The group mind of hybrid teams"](https://journals.sagepub.com/doi/10.1177/02683962241296883)
- [Steve Yegge — "The Anthropic Hive Mind" (Medium, Feb 2026)](https://steve-yegge.medium.com/the-anthropic-hive-mind-d01f768f3d7b)
- Full agent coordination research: [research-2026-02-17-agent-coordination-landscape.md](research-2026-02-17-agent-coordination-landscape.md)
