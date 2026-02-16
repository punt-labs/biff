#!/usr/bin/env bash
# Format biff MCP tool output as a clean who-style table.
# The hook replaces both the UI display and the model's view
# of the tool result, so we pre-format everything here.
#
# tool_response arrives as a JSON-encoded STRING, not an object.
# We must parse it twice: once to extract the string, once to
# read the .result field inside it.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name')

# Only format who output — pass other biff tools through
if [[ "$TOOL" != "mcp__biff__who" ]]; then
  RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')
  jq -n --arg r "$RESULT" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      updatedMCPToolOutput: $r
    }
  }'
  exit 0
fi

# Double-parse: tool_response is a string containing JSON
RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')

# Always start with the header
TABLE=$(printf "NAME      S  TIME   PLAN")

# Parse pipe-separated entries if present
if [[ "$RESULT" == *" + "* || "$RESULT" == *" - "* ]]; then
  IFS='|' read -ra ENTRIES <<< "$RESULT"
  for entry in "${ENTRIES[@]}"; do
    entry=$(echo "$entry" | xargs)  # trim whitespace
    user=$(echo "$entry" | awk '{print $1}')
    flag=$(echo "$entry" | awk '{print $2}')
    time=$(echo "$entry" | awk '{print $3}')
    plan=$(echo "$entry" | awk '{for(i=4;i<=NF;i++) printf "%s ", $i; print ""}' | xargs)
    TABLE=$(printf "%s\n%-9s %s  %s  %s" "$TABLE" "$user" "$flag" "$time" "$plan")
  done
fi

jq -n --arg r "$TABLE" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    updatedMCPToolOutput: $r
  }
}'
