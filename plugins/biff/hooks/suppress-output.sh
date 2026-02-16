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
RESULT=$(printf '%s' "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')

# Empty result — no active sessions
if [[ -z "$RESULT" ]]; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      updatedMCPToolOutput: "No active sessions."
    }
  }'
  exit 0
fi

# Parse pipe-separated entries and compute column width
ROWS=()
NAME_W=4  # minimum width = length of "NAME"

IFS='|' read -ra ENTRIES <<< "$RESULT"
for entry in "${ENTRIES[@]}"; do
  # Trim leading/trailing whitespace without xargs
  entry="${entry#"${entry%%[![:space:]]*}"}"
  entry="${entry%"${entry##*[![:space:]]}"}"
  user=$(printf '%s' "$entry" | awk '{print $1}')
  w=${#user}
  (( w > NAME_W )) && NAME_W=$w
  ROWS+=("$entry")
done

# Build table with dynamic NAME column width
TABLE=$(printf "%-${NAME_W}s  S  TIME   PLAN" "NAME")

for entry in "${ROWS[@]}"; do
  user=$(printf '%s' "$entry" | awk '{print $1}')
  flag=$(printf '%s' "$entry" | awk '{print $2}')
  time=$(printf '%s' "$entry" | awk '{print $3}')
  plan=$(printf '%s' "$entry" | awk '{for(i=4;i<=NF;i++) printf "%s ", $i; print ""}')
  # Trim trailing whitespace from plan
  plan="${plan%"${plan##*[![:space:]]}"}"
  TABLE=$(printf "%s\n%-${NAME_W}s  %s  %s  %s" "$TABLE" "$user" "$flag" "$time" "$plan")
done

jq -n --arg r "$TABLE" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    updatedMCPToolOutput: $r
  }
}'
