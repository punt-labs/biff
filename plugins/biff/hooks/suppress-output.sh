#!/usr/bin/env bash
# Format biff MCP tool output for the UI panel.
#
# updatedMCPToolOutput sets the text displayed in the tool-result panel.
#
# tool_response arrives as a JSON-encoded STRING, not an object.
# We must parse it twice: once to extract the string, once to
# read the .result field inside it.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name')

# read_messages: show a short summary in the panel;
# the model still receives the full response and emits the table.
if [[ "$TOOL" == "mcp__biff__read_messages" ]]; then
  RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')
  if [[ "$RESULT" == "No new messages." ]]; then
    jq -n --arg r "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    # Count data lines (total lines minus header)
    COUNT=$(printf '%s' "$RESULT" | wc -l | tr -d ' ')
    jq -n --arg r "${COUNT} new" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  fi
  exit 0
fi

# who: show a short summary in the panel;
# the model still receives the full response and emits the table.
if [[ "$TOOL" == "mcp__biff__who" ]]; then
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
    jq -n --arg r "${COUNT} online" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  fi
  exit 0
fi

# finger: show username in the panel;
# the model still receives the full response and emits the detail.
if [[ "$TOOL" == "mcp__biff__finger" ]]; then
  RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')
  # Extract username from "Login: <user>" on first line
  USER=$(printf '%s' "$RESULT" | head -1 | sed 's/.*Login: *\([^ ]*\).*/\1/')
  jq -n --arg r "@${USER}" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      updatedMCPToolOutput: $r
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
