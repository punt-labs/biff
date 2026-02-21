#!/usr/bin/env bash
# Format biff MCP tool output for the UI panel.
#
# updatedMCPToolOutput sets the text displayed in the tool-result panel.
# additionalContext passes the full tool data to the model separately,
# so the model can emit the table while the panel stays compact.
#
# tool_response arrives as a JSON-encoded STRING, not an object.
# We must parse it twice: once to extract the string, once to
# read the .result field inside it.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name')

# read_messages: summary in panel, full data via additionalContext.
if [[ "$TOOL" == "mcp__plugin_biff_biff__read_messages" ]]; then
  RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')
  if [[ "$RESULT" == "No new messages." ]]; then
    jq -n --arg r "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    COUNT=$(printf '%s' "$RESULT" | wc -l | tr -d ' ')
    jq -n --arg summary "${COUNT} new" --arg ctx "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $summary,
        additionalContext: $ctx
      }
    }'
  fi
  exit 0
fi

# who: summary in panel, full data via additionalContext.
if [[ "$TOOL" == "mcp__plugin_biff_biff__who" ]]; then
  RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')
  if [[ "$RESULT" == "No sessions." ]]; then
    jq -n --arg r "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    COUNT=$(printf '%s' "$RESULT" | wc -l | tr -d ' ')
    jq -n --arg summary "${COUNT} online" --arg ctx "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $summary,
        additionalContext: $ctx
      }
    }'
  fi
  exit 0
fi

# finger: username in panel, full data via additionalContext.
if [[ "$TOOL" == "mcp__plugin_biff_biff__finger" ]]; then
  RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')
  USER=$(printf '%s' "$RESULT" | head -1 | sed 's/.*Login: *\([^ ]*\).*/\1/')
  jq -n --arg summary "@${USER}" --arg ctx "$RESULT" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      updatedMCPToolOutput: $summary,
      additionalContext: $ctx
    }
  }'
  exit 0
fi

# Pass other biff tools through unchanged
RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')
jq -n --arg r "$RESULT" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    updatedMCPToolOutput: $r
  }
}'
