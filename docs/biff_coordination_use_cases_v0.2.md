# Biff Agentic Team Coordination — Use Case Foundation v0.2

| Field | Value |
|---|---|
| **Project** | Biff (Punt Labs) |
| **Date** | 2026-03-08 |
| **Participants** | Owner, Claude (Anthropic) |
| **Outcome** | Use-Case Foundation v0.2 — 15 use cases, 5 open questions resolved |
| **Methodology** | Jacobson & Cockburn, Use-Case Foundation v1.1 |

---

## System of Interest

Biff — team communication CLI + MCP server for software engineers.
Resurrects the BSD Unix communication vocabulary (`write`, `wall`,
`finger`, `who`, `mesg`, `talk`) as MCP-native slash commands over NATS.

---

## Scope

**In scope (V1):** The 10 shipped commands (v0.17.0), NATS relay
(cross-machine), local relay (single-machine), CLI, Python library API,
mobile surface (iPhone app), GitHub Actions / GitHub Apps as external
actors. Agent-to-agent coordination across tasks and machines via existing
primitives.

**Out of scope (V1):** Process tree (Variant 1 of Z model), cross-repo
discovery, machine topology, dynamic membership, orgs/multi-tenancy, E2E
encryption, `/pair` session sharing, shared agent memory.

---

## Actors

| Actor | Description | Surface |
|-------|-------------|---------|
| **Engineer** | Human at a terminal (Claude Code or CLI) | MCP slash commands, CLI |
| **Mobile Engineer** | Human away from terminal | iPhone app (biff Python lib in SwiftUI) |
| **AI Agent** | Claude Code session doing work autonomously | MCP tools via library API |
| **GitHub** | CI workflow or GitHub App with a biff identity | CLI (`biff wall`, `biff write`) via service token |

All actors share the same identity model: a `user:tty` session registered
in NATS or local relay. The system does not distinguish actor types at the
protocol level.

### Actor Notes

- **Mobile Engineer** connects through the biff Python library embedded in
  a SwiftUI app. The app contains all biff commands and logic — it is not
  raw NATS integration. The transport between the app and the relay is an
  implementation detail (could be the library's relay client, a REST
  gateway, or a lightweight bridge).

- **GitHub** is a user in the biffworld. It authenticates via a service
  token stored in repository secrets. A CI runner registers a session like
  any other actor (`github-actions:ttyN`), posts walls and writes, and its
  session expires when the workflow completes.

---

## Resolved Questions

| # | Question | Resolution |
|---|----------|------------|
| Q1 | Mobile surface protocol | iPhone app embeds the biff Python library in SwiftUI. Not raw NATS. |
| Q2 | GitHub Action identity | GitHub is a user in the biffworld with a service token. |
| Q3 | Agent message polling frequency | Not applicable. Biff already pushes notifications via tool description mutation + `tools/list_changed`. The agent reads ASAP on notification. |
| Q4 | Wall vs. write for CI | Both. `/wall` for team-visible status, `/write @committer` for targeted notification. |
| Q5 | Talk with agents | Talk is synchronous. An agent engages in `/talk` only when it is stopped — waiting on the human, like a human waits on a reply. |

---

## Use Case Inventory

| UC | Title | Primary Actor | Mechanism | Trigger |
|----|-------|--------------|-----------|---------|
| UC1 | Check Who's Working on What | Engineer / Mobile Engineer | `/who` | Starting a session, needs awareness |
| UC2 | Inspect a Teammate's Availability | Engineer / Mobile Engineer | `/finger` | Before sending a message |
| UC3 | Declare What You're Working On | Engineer / AI Agent | `/plan`, `/tty` | Starting a task, switching branches |
| UC4 | Send a Focused Message | Engineer / Mobile Engineer / AI Agent | `/write` | Needs input, handoff, or notification |
| UC5 | Check and Process Incoming Messages | Engineer / Mobile Engineer / AI Agent | `/read` | Push notification or periodic check |
| UC6 | Broadcast a Time-Sensitive Announcement | Engineer / AI Agent / GitHub | `/wall` | Deploy freeze, release, milestone |
| UC7 | Escalate a Decision to a Human | AI Agent | `/write` | Ambiguous choice, risk threshold |
| UC8 | Report Task Completion | AI Agent | `/write`, `/plan` | PR ready, tests passing |
| UC9 | Redirect an Agent's Priorities | Engineer / Mobile Engineer | `/write` | Strategy change, new urgent work |
| UC10 | Answer an Agent's Pending Question | Engineer / Mobile Engineer | `/write` | Reads escalation from agent |
| UC11 | Avoid Duplicate Work Across Agents | AI Agent | `/who`, `/plan` | About to claim a task |
| UC12 | Coordinate File Ownership in a Shared Repo | AI Agent | `/who`, `/write` | Two agents active in same repo |
| UC13 | Enter Do-Not-Disturb Mode | Engineer / AI Agent | `/mesg` | Deep work, long operation |
| UC14 | Have a Synchronous Exchange | Engineer | `/talk` | Needs iterative, immediate back-and-forth |
| UC15 | Notify Team of CI/CD Status | GitHub | `/wall`, `/write` | Workflow completes, fails, or needs attention |

---

## Group A: Presence

### UC1 — Check Who's Working on What

**Primary Actor:** Engineer / Mobile Engineer
**Trigger:** Starting a session, or needs situational awareness
**Preconditions:** Actor has a biff-enabled session (or mobile app connected to relay)

**Basic Course:**

1. Actor invokes `/who`
2. System queries relay for all active sessions in this repo
3. System displays session table: name, tty, idle time, message status,
   host, directory, plan
4. Actor scans the table to understand team state

**Extensions:**

- 2a. No other sessions are active → System displays empty table; actor
  is alone
- 2b. Relay is unreachable (NATS down) → System falls back to local relay
  if available; otherwise returns error
- 3a. A session's heartbeat is stale (idle > threshold) → System still
  displays it with idle time; 30-day TTL means sessions persist

---

### UC2 — Inspect a Teammate's Availability

**Primary Actor:** Engineer / Mobile Engineer
**Trigger:** Before sending a message or assigning work
**Preconditions:** Actor knows the teammate's username

**Basic Course:**

1. Actor invokes `/finger @teammate`
2. System resolves the username to one or more active sessions
3. System displays: display name, all sessions with tty names, idle time,
   message status, host, directory, plan, last active timestamp
4. Actor decides whether and how to contact the teammate

**Extensions:**

- 2a. Username not found in any active session → System reports no active
  sessions for that user
- 2b. Multiple sessions for that user → System displays all; actor
  chooses which tty to target
- 3a. Teammate's message status is `−` (DND) → Actor sees this and may
  defer the message

---

### UC3 — Declare What You're Working On

**Primary Actor:** Engineer / AI Agent
**Trigger:** Starting a task, switching branches, or communicating status

**Basic Course (manual):**

1. Actor invokes `/plan "description"`
2. System stores the plan text against the actor's session
3. System expands any bead IDs in the plan to include issue titles
4. Plan becomes visible in `/who` and `/finger` output for all teammates

**Basic Course (automatic — git hooks):**

1. Actor checks out a branch
2. post-checkout hook fires and invokes `biff plan` with branch name
3. System stores `→ branch-name` as the plan with source `auto`
4. Actor commits
5. post-commit hook fires and invokes `biff plan` with commit summary
6. System stores `✓ commit message` as the plan with source `auto`

**Extensions:**

- 1a. Actor sets plan while an auto-plan exists → Manual plan
  (`source: manual`) takes priority; auto-plan is overwritten
- 2a. Bead ID in plan text is invalid → System stores the raw text
  without expansion
- (auto) 2a. Hook fires but biff is disabled for this repo → Hook exits
  silently, no plan set

**Includes:** `/tty "name"` — Actor names their session for readability.
Typically done once at session start. A precondition for useful `/who`
output when multiple sessions exist.

---

## Group B: Asynchronous Messaging

### UC4 — Send a Focused Message

**Primary Actor:** Engineer / Mobile Engineer / AI Agent
**Trigger:** Needs input, handing off work, or notifying

**Basic Course:**

1. Actor invokes `/write @recipient "message"`
2. System resolves the recipient address (`@user` for broadcast,
   `@user:tty` for targeted)
3. System stores the message in the recipient's inbox on the relay
4. System returns confirmation to the sender
5. Recipient's system fires push notification (tool description mutation +
   `tools/list_changed` for MCP sessions; app notification for mobile)

**Extensions:**

- 2a. Recipient username not found → System returns error; message not
  sent
- 2b. Recipient has multiple sessions and address is `@user` (no tty) →
  Message goes to per-user broadcast inbox; all sessions see it
- 2c. Recipient's message status is off (`/mesg off`) → Message is still
  stored; recipient will see it when they check. Sender is not warned
  (pull model — no delivery receipts)
- 3a. Relay is unreachable → System returns error; actor may retry or
  defer

---

### UC5 — Check and Process Incoming Messages

**Primary Actor:** Engineer / Mobile Engineer / AI Agent
**Trigger:** Push notification (tool description shows unread count), or
session start hook

**Basic Course:**

1. Actor receives push notification indicating unread messages
2. Actor invokes `/read`
3. System retrieves all unread messages from the actor's inbox (broadcast
   and tty-targeted)
4. System displays messages with sender, timestamp, and content
5. System marks messages as read (POP semantics — consumed once, then
   deleted)

**Extensions:**

- 1a. Actor checks proactively without notification → Same flow from
  step 2
- 3a. No unread messages → System reports inbox is empty
- 3b. Messages from multiple senders → System displays all, ordered by
  timestamp
- 5a. Actor wants to reply → Actor uses `/write @sender "reply"` (no
  threading — each message is independent)

---

### UC7 — Escalate a Decision to a Human

**Primary Actor:** AI Agent
**Trigger:** Agent encounters an ambiguous design choice, risk threshold,
or needs approval

**Basic Course:**

1. Agent identifies a decision it cannot or should not make autonomously
2. Agent invokes `/write @human:tty "description of decision needed,
   options, and context"`
3. System delivers message to the human's inbox
4. Human's system fires push notification
5. Human reads the message (UC5) and responds (UC10)

**Extensions:**

- 2a. Human has multiple sessions → Agent targets the specific tty where
  the human is most active (lowest idle time from `/who`)
- 4a. Human is in DND mode → Message queues; agent continues other work
  or blocks depending on its workflow
- 5a. Human doesn't respond within a reasonable time → Agent may escalate
  via `/wall` or continue with a safe default (agent's policy, not
  biff's)

---

### UC8 — Report Task Completion

**Primary Actor:** AI Agent
**Trigger:** PR ready, tests passing, milestone reached

**Basic Course:**

1. Agent completes a unit of work
2. Agent invokes `/write @human "PR #N ready for review: summary"` for
   targeted notification
3. Agent invokes `/plan "done: summary"` to update visible status
4. Human receives push notification and reviews

**Extensions:**

- 2a. Completion is team-relevant (not just the assigning human) → Agent
  uses `/wall "PR #N ready"` in addition to `/write` (see UC6)
- 3a. Agent has more work queued → Agent updates plan to next task rather
  than "done"

---

### UC9 — Redirect an Agent's Priorities

**Primary Actor:** Engineer / Mobile Engineer
**Trigger:** Reviewing progress, strategy change, or new urgent work

**Basic Course:**

1. Engineer checks agent's current status via `/who` or
   `/finger @user:tty`
2. Engineer invokes `/write @user:tty "stop X, switch to Y because Z"`
3. Agent receives push notification and reads the message (UC5)
4. Agent acknowledges by updating `/plan` and acting on the new priority

**Extensions:**

- 1a. Agent has multiple sessions → Engineer uses `/who` to identify the
  right tty
- 3a. Agent is mid-tool-call and cannot read immediately → Message queues;
  agent reads on next natural pause
- 3b. Engineer needs immediate response → Engineer uses `/talk` (UC14)
  instead of `/write`

---

### UC10 — Answer an Agent's Pending Question

**Primary Actor:** Engineer / Mobile Engineer
**Trigger:** Reads an escalation from an agent (UC7)

**Basic Course:**

1. Engineer receives push notification and invokes `/read`; sees the
   agent's question
2. Engineer considers the options presented
3. Engineer invokes `/write @user:tty "decision: option B because reason"`
4. Agent receives push notification, reads the answer, and proceeds

**Extensions:**

- 1a. Multiple questions from multiple agents → Engineer triages by
  timestamp and priority
- 3a. Engineer needs more context before deciding → Engineer invokes
  `/talk @user:tty` (UC14) for real-time exchange
- 3b. Answer applies to the whole team → Engineer uses `/wall` in
  addition to the targeted reply

---

### UC12 — Coordinate File Ownership in a Shared Repo

**Primary Actor:** AI Agent
**Trigger:** Agent discovers another agent is active in the same repo

**Basic Course:**

1. Agent invokes `/who` and sees another agent session in the same repo
   and directory
2. Agent invokes `/write @other:tty "I'm working on module X, can you
   take module Y?"`
3. Other agent receives push notification, reads the message, and responds
   with agreement or counter-proposal
4. Both agents update `/plan` to reflect their agreed ownership
5. Agents proceed on non-overlapping files

**Extensions:**

- 1a. Other agent is on a different machine but same repo → Coordination
  is logical (avoid same branch), not physical (no file conflicts)
- 1b. Other agent is on the same machine, same directory → Higher
  conflict risk; agents should use git worktrees
- 3a. Other agent doesn't respond (stuck, crashed, or slow) → First
  agent proceeds and accepts merge risk, or defers
- 5a. An agent accidentally edits a file the other claimed → Normal git
  conflict resolution; biff doesn't enforce ownership

---

## Group C: Broadcast

### UC6 — Broadcast a Time-Sensitive Announcement

**Primary Actor:** Engineer / AI Agent / GitHub
**Trigger:** Deploy freeze, release, CI failure, milestone, outage

**Basic Course:**

1. Actor invokes `/wall "message" [duration]`
2. System stores the wall post with TTL (default 1h)
3. System delivers the wall to all active sessions in this repo via NATS
4. All teammates see the wall on their status bar line 2 within 0-2s

**Extensions:**

- 1a. Actor wants to clear an existing wall → `/wall clear` removes the
  current wall
- 1b. Actor invokes `/wall` with no args → System displays the current
  wall if one exists
- 2a. Duration exceeds maximum → System caps or rejects
  (implementation-defined)
- 4a. A teammate joins after the wall was posted → Teammate sees the wall
  on next status bar refresh (wall persists in NATS KV until TTL)

---

### UC15 — Notify Team of CI/CD Status

**Primary Actor:** GitHub
**Trigger:** Workflow completes, fails, or needs attention

**Basic Course:**

1. GitHub Action step invokes
   `biff wall "CI: build failed on main — @kai's commit abc123" --duration 2h`
2. GitHub Action step invokes
   `biff write @kai "your commit abc123 broke CI: [link]"`
3. System stores the wall post and the targeted message
4. All active sessions see the CI status on their status bar
5. The committer receives a targeted push notification
6. Engineers and agents react (investigate, pause pushes, etc.)

**Extensions:**

- 1a. Workflow succeeded → GitHub posts a success wall with short TTL
  (e.g., 15m) or skips the wall entirely (team preference)
- 2a. Committer has no active session → Message queues; they see it on
  next login
- 3a. GitHub's service token is missing or expired → Action step fails;
  CI output shows authentication error

---

## Group D: Real-Time Conversation

### UC14 — Have a Synchronous Exchange

**Primary Actor:** Engineer
**Trigger:** Needs iterative, immediate back-and-forth (design discussion,
debugging, live coordination)

**Basic Course:**

1. Engineer invokes `/talk @recipient "opening message"`
2. System establishes a talk session between the two parties via NATS
   pub/sub
3. Recipient sees the incoming talk message on their status bar
4. Recipient accepts by replying via `/talk`
5. Both parties exchange messages in real-time (0-2s latency)
6. Either party invokes `/talk end` to close the conversation

**Extensions:**

- 2a. Recipient is offline → System returns error; no talk session
  established
- 3a. Recipient is in DND mode (`/mesg off`) → Talk invitation may still
  arrive (implementation-defined); recipient chooses whether to engage
- 5a. Conversation stalls (one party stops responding) → No automatic
  timeout; either party can `/talk end`
- 5b. Agent is the recipient → Agent engages in talk only when stopped
  (waiting on input, between tasks). Agent uses `/talk_listen` to block
  until a message arrives, then responds. Talk is synchronous for agents —
  like a human waiting on a reply in this conversation right now.

---

## Group E: Focus Management

### UC13 — Enter Do-Not-Disturb Mode

**Primary Actor:** Engineer / AI Agent
**Trigger:** Entering deep work, running a long operation, or needing
uninterrupted focus

**Basic Course:**

1. Actor invokes `/mesg off`
2. System updates the actor's session to show message status `−`
3. Teammates see the `−` status in `/who` and `/finger` output
4. Messages sent to this actor still queue but push notifications are
   suppressed
5. When ready, actor invokes `/mesg on` to resume

**Extensions:**

- 3a. A teammate sends a message anyway → Message queues normally; it
  will be visible when actor does `/read`
- 4a. A `/wall` is posted → Wall still appears on status bar (walls
  bypass DND — they are team-critical)
- 5a. Actor forgets to re-enable → Status persists until session ends or
  actor acts; no automatic timeout

---

## Group F: Agent-to-Agent Coordination

### UC11 — Avoid Duplicate Work Across Agents

**Primary Actor:** AI Agent
**Trigger:** About to claim a task

**Basic Course:**

1. Agent checks `/who` to see all active sessions and their plans
2. Agent scans plans for overlap with the task it intends to claim
3. No overlap found → Agent sets `/plan "task description"` and proceeds
4. Other agents checking `/who` later see this plan and avoid duplicating

**Extensions:**

- 2a. Another agent's plan overlaps → Agent picks a different task or
  invokes `/write @other:tty` to negotiate
- 2b. Plan text is ambiguous (can't tell if it overlaps) → Agent invokes
  `/finger @other` for more context, or `/write` to ask
- 4a. Two agents race and both claim the same task simultaneously → No
  lock mechanism; both proceed and discover the conflict at git level
  (duplicate PR, merge conflict). Biff is coordination infrastructure,
  not a distributed lock.

---

## Deferred Questions

These are implementation details that do not affect the use case
specifications. They are noted for later elaboration.

| # | Topic | Notes |
|---|-------|-------|
| D1 | Mobile app transport | Python library in SwiftUI — exact bridging mechanism (embedded interpreter, REST gateway, etc.) deferred to mobile project |
| D2 | GitHub service token provisioning | How the token is issued, scoped, and rotated — deferred to security design |
| D3 | Wall TTL defaults per actor | Should GitHub Actions walls have different default TTLs than human walls? Team preference, not protocol. |

---

## Status of Open Questions

No open questions remain for sign-off. Five questions were raised in
Draft 0.1 and resolved in Draft 0.2 (see Resolved Questions table above).
Three implementation topics are deferred, noted as non-blocking.

---

## Appendix A: Implementation Plan

This appendix maps each use case to its current implementation status and
identifies what can be enabled now (via hooks, configuration, or
documentation) versus what requires development work.

### Implementation Tiers

| Tier | Description | Effort |
|------|-------------|--------|
| **T0: Shipped** | Already works today with no changes | None |
| **T1: Hooks** | Achievable via Claude Code hooks and existing commands | Days |
| **T2: Configuration** | Requires new config, credentials, or CI workflow files | Days |
| **T3: Development** | Requires new code (library, CLI, or server changes) | Weeks |

### Use Case Status Matrix

| UC | Title | Tier | Status | Notes |
|----|-------|------|--------|-------|
| UC1 | Check Who's Working on What | **T0** | Shipped | `/who` works for Engineer and AI Agent today. Mobile requires T3. |
| UC2 | Inspect a Teammate's Availability | **T0** | Shipped | `/finger` works for Engineer and AI Agent today. Mobile requires T3. |
| UC3 | Declare What You're Working On | **T0** | Shipped | Both manual (`/plan`) and automatic (git hooks) paths work today. |
| UC4 | Send a Focused Message | **T0** | Shipped | `/write` works for Engineer and AI Agent today. Mobile requires T3. |
| UC5 | Check and Process Incoming Messages | **T0** | Shipped | `/read` works. Push notifications via tool description mutation already implemented. |
| UC6 | Broadcast a Time-Sensitive Announcement | **T0** | Shipped | `/wall` works for Engineer and AI Agent today. GitHub actor requires T2. |
| UC7 | Escalate a Decision to a Human | **T1** | Hook | Agent can `/write` today, but nothing *prompts* the agent to escalate. Needs a hook or agent instruction pattern. |
| UC8 | Report Task Completion | **T1** | Partial | PostToolUse hook on PR creation already suggests `/wall`. Can extend to auto-`/write` the assigning human. |
| UC9 | Redirect an Agent's Priorities | **T0** | Shipped | Human `/write`s to agent; agent's push notification triggers `/read`. Works today. |
| UC10 | Answer an Agent's Pending Question | **T0** | Shipped | Human `/write`s back; agent reads on notification. Works today. |
| UC11 | Avoid Duplicate Work Across Agents | **T1** | Hook | Agent can `/who` + `/plan` today, but nothing prompts it to check before claiming work. Needs SessionStart hook guidance or agent instructions. |
| UC12 | Coordinate File Ownership | **T1** | Partial | SessionStart collision detection already warns about shared worktrees. Extending to suggest `/write` negotiation is a hook change. |
| UC13 | Enter Do-Not-Disturb Mode | **T0** | Shipped | `/mesg on\|off` works today. |
| UC14 | Have a Synchronous Exchange | **T0** | Shipped | `/talk` + `/talk_listen` + `/talk_end` work today. |
| UC15 | Notify Team of CI/CD Status | **T2** | Config | Requires GitHub Actions workflow steps + service token provisioning. No biff code changes needed. |

### Summary by Tier

| Tier | Count | Use Cases |
|------|-------|-----------|
| **T0: Shipped** | 9 | UC1, UC2, UC3, UC4, UC5, UC9, UC10, UC13, UC14 |
| **T1: Hooks** | 4 | UC7, UC8, UC11, UC12 |
| **T2: Configuration** | 1 | UC15 |
| **T3: Development** | 1 | Mobile surface (cross-cuts UC1, UC2, UC4, UC5, UC9, UC10) |

---

### T1: Hook Implementations

These use cases work with existing biff commands but need **behavioral
nudges** — hooks or agent instructions that prompt the right action at
the right time.

#### UC7 — Escalate a Decision to a Human

**Hook:** `Stop` event.

When an agent finishes a turn and has identified an unresolved decision
(e.g., mentioned "I need to decide" or "unclear whether" in its
reasoning), the Stop hook can inject guidance:

```text
"You identified an unresolved decision. Consider escalating via
/write @human:tty with the options and trade-offs."
```

**Alternative:** Agent system prompt instructions. Biff's
`AGENT_WORKFLOW.md` already documents this pattern. A SessionStart hook
could inject the escalation protocol into the agent's context on startup.

**Claude Code hook event:** `Stop` (can prevent stop, inject
`additionalContext` to continue).

#### UC8 — Report Task Completion

**Hook:** `PostToolUse` on PR creation (already exists) + `Stop` event.

The existing PostToolUse hook on
`create_pull_request` already suggests `/wall`. Extend it to also suggest
`/write @human` with the PR link. Additionally, a `Stop` hook at session
end could check if a PR was created during the session and remind the
agent to announce it if it hasn't already.

**Claude Code hook events:**

- `PostToolUse` with matcher `create_pull_request` (exists today)
- `Stop` (new addition)

#### UC11 — Avoid Duplicate Work Across Agents

**Hook:** `SessionStart` with matcher `startup`.

The existing SessionStart hook already checks for worktree collisions.
Extend it to also inject a behavioral directive:

```text
"Other sessions are active in this repo. Before starting work, run
/who to check what others are working on. Set /plan before beginning
to avoid duplicate effort."
```

This leverages the existing collision detection logic in `handle_session_start()`.

**Claude Code hook event:** `SessionStart` with matcher `startup`
(extend existing handler).

#### UC12 — Coordinate File Ownership in a Shared Repo

**Hook:** `SessionStart` with matcher `startup` (same as UC11).

The collision detection already warns when multiple sessions share a
worktree. Extend the warning to suggest `/write @other:tty` to negotiate
file ownership, and recommend git worktrees for isolation.

**Claude Code hook event:** `SessionStart` with matcher `startup`
(extend existing handler, same change as UC11).

---

### T2: Configuration

#### UC15 — Notify Team of CI/CD Status

**What's needed:**

1. **Service token:** A NATS credential scoped to the repo's biff
   namespace, stored as a GitHub Actions secret (`BIFF_NATS_TOKEN`).
   The token allows the CI runner to register a session and post
   messages.

2. **Workflow steps:** Add steps to `.github/workflows/*.yml`:

   ```yaml
   - name: Notify team of failure
     if: failure()
     run: |
       biff wall "CI: build failed on ${{ github.ref_name }} — ${{ github.actor }}" --duration 2h
       biff write @${{ github.actor }} "Your push to ${{ github.ref_name }} broke CI: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
     env:
       BIFF_NATS_TOKEN: ${{ secrets.BIFF_NATS_TOKEN }}
   ```

3. **Identity configuration:** A `.biff` config or environment variables
   that identify the CI runner as `github-actions` user. The session
   registers on workflow start and expires on completion.

**No biff code changes required.** The CLI already supports all the
commands. The work is credential provisioning and workflow configuration.

---

### T3: Development

#### Mobile Surface (iPhone App)

**Cross-cuts:** UC1, UC2, UC4, UC5, UC9, UC10

**What's needed:**

1. **Python-to-Swift bridge:** Embed the biff Python library in a SwiftUI
   app. Options include PythonKit, an embedded Python interpreter, or a
   thin REST gateway that wraps the library.

2. **App UI:** SwiftUI views for `/who` (team list), `/finger` (detail),
   `/write` (compose), `/read` (inbox), and push notifications.

3. **Session management:** The app registers a biff session on launch
   and deregisters on background/terminate. Push notifications when
   messages arrive while the app is backgrounded (requires APNs bridge
   or polling).

4. **Authentication:** The app needs NATS credentials and user identity.
   Could reuse the same `.biff` config or have its own provisioning flow.

**This is a separate project** (like `quarry-menubar` is to `quarry`).
The biff library API is already designed for this — `CliContext` +
`commands.*` functions are surface-agnostic. The mobile app is a new
consumption surface, not a biff change.

---

### Implementation Sequence

```text
Phase 1: Hooks (T1) — Immediate
├── Extend SessionStart to inject coordination guidance (UC11, UC12)
├── Extend PostToolUse on PR creation to suggest /write (UC8)
├── Add Stop hook for escalation nudges (UC7)
└── Update AGENT_WORKFLOW.md with use case references

Phase 2: CI Integration (T2) — Near-term
├── Provision NATS service token for GitHub Actions
├── Add biff notification steps to CI workflows (UC15)
└── Document GitHub-as-actor setup in README

Phase 3: Mobile App (T3) — Future project
├── Evaluate Python-in-Swift bridging options
├── Build SwiftUI app with biff library integration
└── Ship as companion app (like quarry-menubar)
```

---

<!-- End of Use-Case Foundation v0.2 -->
