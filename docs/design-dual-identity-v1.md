# Design: Dual Identity in Sessions (v1)

> First pass — models the human as the anchor, agent as annotated role.

## Problem Statement

In a Claude Code session, there are two entities — the human user and the AI
agent. But biff models them as one identity. This creates three problems:

### 1. Authorship Ambiguity

Eric receives: `kai: here's what I found in your code review`

Was that Kai the human, or Kai's agent? Eric has no way to know. This matters —
you interact differently with a human vs an AI response.

### 2. Addressing Ambiguity

Eric sends: `/write @kai "can you run the tests on feature-x?"`

Is this for the human or the agent? Currently both see it (human via status
line, agent when `/read` is called). There's no way to express intent.

### 3. Presence Ambiguity

`/who` shows `kai` on `tty1` with plan "reviewing PR #42". But did Kai set that
plan, or did the agent auto-set it? Is Kai at the keyboard, or did they walk
away while the agent runs autonomously?

## Current Model

One process, one identity, one session key (`user:tty`). Every tool handler
reads `state.config.user` — there's no per-call identity. The `tty`
disambiguates multiple windows, not multiple entities within a window.

| Entity | Can send | Can receive | Has presence | Sets plan |
|--------|:--------:|:-----------:|:------------:|:---------:|
| Human  | via slash commands | sees status line | yes | yes (manual) |
| Agent  | via tool calls | sees tool results | implicit only | yes (auto) |

## Design Space

Three models considered, each with different trade-offs.

### Model A: One Entity, Annotated Role (Recommended)

The session stays `kai`. Messages carry a `role` field indicating who composed
them.

```text
/who
kai     tty1    reviewing PR #42 (pair)    idle 2m

/read
kai        10:31  here's what I found...          [agent]
kai        10:28  hey, can you look at this?      [human]
```

- **Identity**: unchanged (`kai`)
- **Session**: unchanged (`kai:tty`)
- **Messages**: `Message` gains `from_role: "human" | "agent"`
- **Presence**: `UserSession` gains `mode: "solo" | "pair"`
- **Addressing**: `@kai` reaches both; no role-specific addressing

**Pros**: Minimal model change. Preserves the Unix metaphor (one user, one
terminal). Honest about the reality — it IS kai's session, the agent acts on
kai's behalf.

**Cons**: Can't address the agent specifically. Can't send the human a message
the agent won't see (and vice versa).

### Model B: Two Entities, Linked

The Claude Code session registers two presences: `kai` (human) and `kai/claude`
(agent). They share context but are separately addressable.

```text
/who
kai          tty1    lunch break                idle 15m
kai/claude   tty1    reviewing PR #42           idle 0m

/write @kai "lunch?" → human only
/write @kai/claude "run the tests" → agent only
/write @kai:tty1 "status?" → both (session-level)
```

- **Identity**: `kai` + synthetic `kai/claude`
- **Session**: two session keys sharing a tty
- **Messages**: addressed to either or both
- **Presence**: separate entries, separate plans, separate idle times

**Pros**: Full addressability. Clear authorship. Human idle 15m but agent idle
0m tells you Kai walked away but work continues.

**Cons**: Doubles the presence surface. Synthetic identity doesn't go through
identity resolution chain. Routing complexity.

### Model C: Session Mode Toggle

The session is `kai`, but there's a mode flag for who's "driving."

```text
/who
kai     tty1    reviewing PR #42    agent-driving    idle 0m
```

- **Identity**: unchanged
- **Presence**: gains `driver: "human" | "agent"`
- **Messages**: `from_role` inferred from current driver
- **Addressing**: `@kai` always; mode is informational only

**Pros**: Simple. Informational.

**Cons**: Doesn't solve authorship. Doesn't enable selective addressing. The
"driver" concept doesn't match pair programming reality.

## Recommendation

Model A is the right starting point:

1. Solves the most important problem (authorship transparency) with minimal
   model change
2. Honest about the relationship — agent acts on behalf of the human
3. Follows Unix metaphor: one user, one terminal, annotated speech
4. Forward-compatible — can evolve to Model B if needed

### Required Changes for Model A

| Component | Change |
|-----------|--------|
| `Message` model | Add `from_role: Literal["human", "agent"]` |
| `UserSession` model | Add `mode: Literal["solo", "pair"]` |
| `write` tool handler | Detect caller context |
| `read` display | Show role indicator |
| `/who` display | Show `(pair)` suffix |
| `/finger` display | Show mode and plan source |

### Open Question

How does the tool handler know if the human or the agent invoked it? In MCP,
all tool calls come from the model. When the human types `/write @eric "hey"`,
that becomes a slash command that the model executes as a tool call. There's no
wire-level distinction.

## Limitations Identified

This version anchors on the human as the session owner. It does not account for:

- Subagents (multiple agents in one session)
- Headless sessions (no human at all)
- Agent teams (multiple top-level agents)
- The human not being necessary

These limitations led to v2.
