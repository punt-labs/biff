# Biff Design Decision Log

This file is the authoritative record of design decisions, prior approaches, and their outcomes. **Every design change must be logged here before implementation.**

## Rules

1. Before proposing ANY design change, consult this log for prior decisions on the same topic.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome.

---

## DES-001: Plugin Hook Architecture — Display Path

**Date:** 2026-02-16
**Status:** SETTLED
**Topic:** How biff tool output reaches the user in Claude Code

### Design

Three-layer architecture:

1. **PostToolUse hook** (`suppress-output.sh`) — Runs after every biff MCP tool call.
   - Sets `updatedMCPToolOutput` (compact summary shown in tool-result panel).
   - Sets `additionalContext` (full output passed to the model as context).
2. **Skill command prompt** (e.g., `who.md`) — Instructs the LLM to call the tool and emit the additionalContext verbatim.
3. **MCP tool** — Returns the raw data.

### Why This Design

- `/write`, `/mesg`, `/plan` need NO model output — the hook's `updatedMCPToolOutput` is sufficient. Their skill prompts say "Do not send any text after the tool call."
- `/who`, `/finger`, `/read` need the model to emit the full output because `updatedMCPToolOutput` shows only a summary (e.g., "3 online"). The full data travels via `additionalContext` and the skill prompt tells the model to emit it verbatim.

### Known Problem

The LLM sometimes reformats the `additionalContext` output (adds markdown tables, removes unicode characters, adds code fence boxes). This is an **unsolved prompt engineering problem** — not a design problem. The architecture is correct; the skill prompt wording needs iteration.

### Prior Approaches Tried (2026-02-16)

| Attempt | Prompt wording | Outcome |
|---------|---------------|---------|
| Original | "Emit the full session table. Do not add commentary or code fences." | LLM reformats as markdown table with boxes |
| v2 | "emit the tool output exactly as returned — character for character" | LLM still adds boxes |
| v3 | Added "Do not ... convert to markdown tables, or add boxes around the output." | LLM still adds boxes after reload+clear |
| v4 | Added "including the leading ▶ unicode character" | LLM still drops ▶ in some sessions |

**What does NOT work:** Negative constraint lists — the LLM finds new ways to reformat.

**What has NOT been tried:**
- Positive-only instruction (e.g., "Your entire response must be exactly: {content}")
- Putting the raw text directly in the skill prompt as a template
- Using a different hook field or mechanism

### Rejected Approach: Collapsing to updatedMCPToolOutput Only

Attempted 2026-02-16 — putting full data in `updatedMCPToolOutput` and telling the model to emit nothing (matching `/write` pattern). **Rolled back immediately.** This changes the display architecture for data-emitting commands and was attempted without consulting prior design decisions or logging. The summary-in-panel + full-data-via-additionalContext split was deliberate.

---

## DES-002: Session Key Format

**Date:** 2026-02-16
**Status:** SETTLED
**Topic:** How sessions are identified

### Design

Session keys are composite `{user}:{tty}` strings. TTY is an 8-char hex random ID generated at server startup. This is the fundamental identifier throughout the stack — relay, storage, tools, tests.

- Broadcast: `/write @user` — delivers to all sessions of that user
- Targeted: `/write @user:tty` — delivers to one session
- Per-TTY inboxes: `inbox-{user}-{tty}.jsonl` (local) / NATS subjects per session

### Why

Supports multiple concurrent sessions per user (human + agents in same repo). The TTY metaphor maps cleanly to the Unix communication vocabulary.
