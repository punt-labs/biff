#!/usr/bin/env bash
# Suggest /plan after claiming a bead in biff-enabled projects.
#
# PostToolUse hook. Matches ALL Bash tool calls — must exit fast
# on non-matching commands.  Only fires when:
#   1. Working directory has a .biff marker
#   2. Command contains "bd update" + "--status" + "in_progress"
#   3. Command succeeded (tool_response contains ✓)
#
# Light-touch: does NOT parse bd output or run bd show.
# The agent already has context to construct the /plan message.
set -euo pipefail

# Gate: only fire in biff-enabled projects
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0

INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Fast exit: not a bead claim
[[ "$CMD" =~ bd[[:space:]]+update.*--status[=[:space:]]in_progress ]] || exit 0

# Check the command succeeded (tool_response should contain ✓)
RESPONSE=$(echo "$INPUT" | jq -r '.tool_response // ""')
[[ "$RESPONSE" == *"✓"* ]] || exit 0

jq -n '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: "You just claimed a bead. Set your dotplan so teammates can see what you are working on: /plan <description of the work>"
  }
}'
