# Agent Workflow

Biff is MCP-native, so it does not distinguish between human and agent sessions. This guide covers patterns for working with multiple agents and mixed human+agent teams.

## Agents as Team Members

When an agent starts a Claude Code session in a biff-enabled repo, it automatically:

1. Registers a presence entry (visible in `/who`)
2. Gets assigned a TTY identifier (via `/tty`)
3. Sets its plan from the current git branch
4. Checks for unread messages

From that point, the agent shows up in `/who` alongside humans:

```text
> /who

▶  NAME    TTY   IDLE  S  HOST       DIR                        PLAN
   @kai    tty1  0:03  +  m2-mb-air  /Users/kai/code/myapp      refactoring auth module
   @kai    tty2  0:00  +  m2-mb-air  /Users/kai/code/myapp      → feat/add-tests
   @kai    tty3  0:01  +  m2-mb-air  /Users/kai/code/myapp      → fix/flaky-ci
```

Here, `tty1` is the human, `tty2` and `tty3` are agents. Each has a distinct TTY, targetable via `/write @kai:tty2`.

## Two Coordination Planes

### Logical Plane (Cross-Machine)

**Problem:** Two agents pick up the same task.

**Solution:** `/plan` shows what each session is working on. `/who` shows all plans across all machines. Before claiming work, an agent can check `/who` to see if someone else is already on it.

```text
> /plan "biff-ka4: post-checkout hook: update plan from branch"
```

Bead IDs auto-expand, so teammates see the full task title, not just an opaque ID.

### Physical Plane (Same-Machine)

**Problem:** Two agents edit the same files and create merge conflicts.

**Solution:** `/who` shows host and directory per session. When two sessions share the same machine and directory, they should coordinate:

1. Check `/who` for other sessions in the same directory
2. Use `/write @user:tty` to coordinate who works on what
3. Create git worktrees for isolation when needed

```text
> /write @kai:tty3 "I'm working on auth.py, can you take tests?"
```

## Communication Patterns

### Agent Asks Human for a Decision

An agent hits an ambiguous design choice and needs human input:

```text
Agent (tty2):  /write @kai:tty1 "auth module: should session tokens expire after 1h or 24h? need to decide before implementing refresh logic"
```

The human sees this in their status bar or next `/read`.

### Human Steers an Agent

A human reviews an agent's work and redirects:

```text
Human (tty1):  /write @kai:tty2 "skip the migration for now, focus on the API tests first"
```

### Agent Reports Completion

An agent finishes a task and announces it:

```text
Agent (tty2):  /wall "PR #47 ready for review: auth module refactor"
```

All team members see this on their status bar.

### Real-Time Agent Conversation

Use `/talk` for back-and-forth with an agent:

```text
Human (tty1):  /talk @kai:tty2 "what's the status on the auth refactor?"
```

Replies appear on the status bar automatically. The human can reply with `/write` and close with `/talk end`.

## Workflow Hooks

Biff's git hooks automate coordination overhead:

| Hook | Trigger | Effect |
|------|---------|--------|
| **post-checkout** | Branch switch | Plan updates to `→ feature/auth` |
| **post-commit** | Commit | Plan updates to `✓ feat: add auth` |
| **pre-push** | Push to main | Suggests `/wall` announcement |
| **SessionStart** | New session | Auto-assigns TTY, sets plan from branch, checks messages |
| **SessionEnd** | Session closes | Cleans up presence immediately |

These hooks fire for both human and agent sessions identically.

## Best Practices

- **Name your agents.** Use `/tty work` or `/tty tests` so `/who` output is readable, not just `tty1`, `tty2`, `tty3`.
- **Set plans.** Agents should `/plan` what they're doing so humans can see at a glance.
- **Use targeted messages.** `/write @kai:tty2` reaches a specific agent. `/write @kai` broadcasts to all of kai's sessions.
- **Use worktrees for isolation.** When multiple agents work in the same repo, create git worktrees so they don't step on each other's files.
- **Use `/wall` for milestones.** PRs opened, CI failures, deploy freezes --- anything the whole team should know.

## What Biff Is Not

Biff is the human+agent communication layer, not an agent orchestration framework. For pure agent-to-agent coordination (shared memory, high-throughput task dispatch, swarm orchestration), use dedicated tools like claude-flow or other agent frameworks. Biff serves the place where humans see what their agents are doing, and agents can reach a human when they need one.
