#!/usr/bin/env bash
# Replace biff MCP tool output with empty string so Claude Code
# only displays the formatted result from the command prompt.
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","updatedMCPToolOutput":""}}
EOF
