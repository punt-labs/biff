# Hook Output Feedback

## Scope

This note reviews the current biff MCP output path against:

- `DESIGN.md` DES-001
- `punt-kit/standards/hooks.md`
- `punt-kit/standards/plugins.md`
- the current `hooks/suppress-output.sh` implementation
- the current Claude Code MCP rendering path in `claude-code-main`

## Bottom Line

The high-level architecture is still correct.

The two-channel display model in `DESIGN.md` should stay:

- `updatedMCPToolOutput` for a compact panel summary
- `additionalContext` for full multiline data that the model emits verbatim

This is still the right response to Claude Code's UI behavior. In the current Claude Code tree, MCP text still goes through the generic text renderer, which truncates non-verbose output to a small number of lines and shows the `Ctrl+O` expansion hint. There is still no public MCP field that forces always-expanded multiline rendering in the main prompt view.

## What Is Working

### 1. DES-001 is still valid

The reasoning in `DESIGN.md` holds up. For data commands like `/who`, `/finger`, `/read`, and `/last`, pushing the full formatted table into `updatedMCPToolOutput` would regress to the panel truncation problem. The `summary in panel + full data via additionalContext` split remains the correct design.

### 2. MCP `instructions` plus command prompts is the right prompt strategy

Moving formatting guidance into the MCP server `instructions` field was the right fix. The per-command prompts reinforce the behavior, but the server-level `instructions` field provides the prior-turn conditioning that made verbatim emission reliable.

### 3. Silent commands should stay silent

The `/write` pattern is good. Single-line confirmations belong in the tool-result panel, and the model should not repeat them.

## Main Problems

### 1. `suppress-output.sh` is now the architectural weak point

The current hook is doing real business logic in bash:

- reads stdin with `INPUT=$(cat)`
- unpacks payloads with `jq`
- derives summaries with `grep`, `head`, `cut`, and `sed`
- implements all tool-specific output policy inline

That conflicts with the current hook standard in `punt-kit/standards/hooks.md`, which says shell hooks should be thin gates, should not consume stdin themselves, and should delegate real logic to Python handlers.

It is also the same class of stdin/pipeline fragility that recently caused hard-to-debug hook hangs elsewhere. biff already has the hardened non-blocking stdin reader in `src/biff/hook.py`; `suppress-output.sh` is the exception.

### 2. The hook is parsing presentation text instead of semantic data

The CLI command layer already has a structured result shape:

- `CommandResult(text, json_data, error)`

Commands like `who`, `read`, and `last` already compute `json_data`, but the MCP tool layer currently returns only formatted strings. That forces the hook to recover counts and identities from rendered text.

This is brittle. Changes to table layout, header text, spacing, or empty-state wording can silently break the hook summaries.

### 3. Test coverage is too narrow for a load-bearing boundary

The current suppress-output tests only cover the `read_messages` counting path. That is not enough for a layer that every MCP tool depends on.

The standards say every MCP tool must have a handler and panel format. The tests should enforce that as a contract.

### 4. Minor prompt inconsistency

Most data commands say "emit the tool output exactly as returned." `last.md` special-cases `"No session history."` into `"No session history available."` That is not a serious bug, but it weakens the otherwise clean verbatim-output doctrine.

## Recommended Direction

### 1. Rewrite suppression as thin shell -> Python handler

Keep `hooks/suppress-output.sh`, but reduce it to a gate/dispatcher that calls a Python hook entrypoint. Put all parsing and policy in Python, using the existing non-blocking stdin helper pattern.

This aligns biff with its own hook standard and removes the biggest source of shell/pipeline brittleness.

### 2. Introduce a structured display envelope

Stop making the hook infer summaries from formatted text. Instead, give the hook a structured result envelope it can consume directly. For example:

- `panel_summary`
- `full_text`
- `assistant_mode` (`silent` or `emit_full_text`)
- `data`
- `error`

Important: this structure is for hook consumption and internal policy, not because Claude Code will render it richly. Current Claude Code behavior still stringifies structured MCP output in the UI.

### 3. Add contract tests for every MCP tool

Add a table-driven test suite that covers:

- every MCP tool
- success and error cases
- empty states
- string, object, and content-array `tool_response` shapes
- summary text
- whether `additionalContext` is present or absent

This should become the enforcement point for the "every tool has a panel format" rule.

### 4. Keep the current prompt split

Do not collapse everything into `updatedMCPToolOutput` only. Do not remove the MCP `instructions` field. Those parts are the correct lessons from the earlier iterations.

## Concrete Recommendation

If only one thing is changed, it should be this:

1. move `suppress-output.sh` logic into Python
2. use the existing non-blocking hook input path
3. test every tool handler

If a second thing is changed, it should be this:

4. stop scraping formatted text and pass structured display data into the suppression layer

That keeps the architecture that is working while removing the part most likely to become fragile again.
