#!/usr/bin/env bash
# Suggest /wall announcement after PR create/merge in biff-enabled projects.
#
# PostToolUse hook. Matches GitHub MCP create_pull_request and
# merge_pull_request tools. Only fires when the working directory is
# inside a repo that has a .biff marker file.
#
# Output: additionalContext only (no updatedMCPToolOutput), so the
# original GitHub tool response is preserved in the panel.
set -euo pipefail

# Gate: only fire in biff-enabled projects
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ -f "$REPO_ROOT/.biff" ]] || exit 0
BIFF_LOCAL="$REPO_ROOT/.biff.local"
if [[ -f "$BIFF_LOCAL" ]]; then
  grep -qE '^enabled\s*=\s*true' "$BIFF_LOCAL" || exit 0
else
  exit 0
fi

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name')
TOOL_NAME="${TOOL##*__}"

if [[ "$TOOL_NAME" == "create_pull_request" ]]; then
  TITLE=$(echo "$INPUT" | jq -r '.tool_input.title // ""')
  # tool_response may be a JSON string or object; handle both
  PR_NUM=$(echo "$INPUT" | jq -r '
    (.tool_response | if type == "string" then fromjson else . end)
    .number // empty
  ' 2>/dev/null) || PR_NUM=""
  [[ -z "$PR_NUM" || -z "$TITLE" ]] && exit 0
  MSG="Created PR #${PR_NUM}: ${TITLE}"

elif [[ "$TOOL_NAME" == "merge_pull_request" ]]; then
  PR_NUM=$(echo "$INPUT" | jq -r '.tool_input.pullNumber // .tool_input.pull_number // ""')
  TITLE=$(echo "$INPUT" | jq -r '.tool_input.commit_title // ""')
  [[ -z "$PR_NUM" ]] && exit 0
  if [[ -n "$TITLE" ]]; then
    MSG="Merged PR #${PR_NUM}: ${TITLE}"
  else
    MSG="Merged PR #${PR_NUM}"
  fi

else
  exit 0
fi

jq -n --arg msg "$MSG" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: ("This team uses biff for communication. Consider announcing to the team: /wall " + $msg)
  }
}'
