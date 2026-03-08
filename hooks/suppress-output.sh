#!/usr/bin/env bash
# Format biff MCP tool output for the UI panel.
#
# Two-channel display (see punt-kit/patterns/two-channel-display.md):
#   updatedMCPToolOutput  -> compact panel line (max 80 cols)
#   additionalContext     -> full data for the model to reference
#
# No `set -euo pipefail` — hooks must degrade gracefully on
# malformed input rather than failing the tool call.

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
TOOL_NAME="${TOOL##*__}"

# Single-pass unpack: handles string-encoded, array, or object responses.
RESULT=$(printf '%s' "$INPUT" | jq -r '
  def unpack: if type == "string" then (fromjson? // .) else . end;
  if (.tool_response | type) == "array" then
    (.tool_response[0].text // "" | unpack)
  else
    (.tool_response | unpack)
  end
  | if type == "object" and has("result") then (.result | unpack) else . end
' 2>/dev/null)

# Fallback: if unpack failed or yielded nothing, use raw tool_response.
if [[ -z "$RESULT" ]]; then
  RESULT=$(printf '%s' "$INPUT" | jq -r '.tool_response // empty' 2>/dev/null)
  [[ -z "$RESULT" ]] && RESULT="(no output)"
fi

emit() {
  local summary="$1" ctx="$2"
  jq -n --arg summary "$summary" --arg ctx "$ctx" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      updatedMCPToolOutput: $summary,
      additionalContext: $ctx
    }
  }'
}

emit_simple() {
  local summary="$1"
  jq -n --arg summary "$summary" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      updatedMCPToolOutput: $summary
    }
  }'
}

# ── Error guard: surface tool errors directly ────────────────────────
ERROR_MSG=$(printf '%s' "$RESULT" | jq -r '.error // empty' 2>/dev/null)
if [[ -n "$ERROR_MSG" ]]; then
  emit_simple "error: ${ERROR_MSG}"
  exit 0
fi

# ── who ──────────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "who" ]]; then
  if [[ "$RESULT" == "No sessions." ]]; then
    emit_simple "$RESULT"
  else
    COUNT=$(printf '%s' "$RESULT" | grep -c '^ *@')
    emit "${COUNT} online" "$RESULT"
  fi
  exit 0
fi

# ── finger ───────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "finger" ]]; then
  USER=$(printf '%s' "$RESULT" | head -1 | sed 's/.*Login: *\([^ ]*\).*/\1/')
  emit "@${USER}" "$RESULT"
  exit 0
fi

# ── read_messages ────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "read_messages" ]]; then
  if [[ "$RESULT" == "No new messages." ]]; then
    emit_simple "$RESULT"
  else
    # Data rows start with 3-space indent + bare username (no @).
    # Header row starts with ▶. Skip it.
    COUNT=$(printf '%s' "$RESULT" | grep -c '^   [a-zA-Z0-9]')
    emit "${COUNT} new" "$RESULT"
  fi
  exit 0
fi

# ── write ────────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "write" ]]; then
  if printf '%s' "$RESULT" | grep -Eqi 'Message sent|Delivered'; then
    emit_simple "sent"
  else
    emit_simple "$RESULT"
  fi
  exit 0
fi

# ── plan ─────────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "plan" ]]; then
  PLAN=$(printf '%s' "$RESULT" | head -1 | cut -c1-60)
  emit_simple "${PLAN}"
  exit 0
fi

# ── last ─────────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "last" ]]; then
  if [[ "$RESULT" == "No session history." ]]; then
    emit_simple "$RESULT"
  else
    COUNT=$(printf '%s' "$RESULT" | grep -c '^ *@')
    emit "${COUNT} sessions" "$RESULT"
  fi
  exit 0
fi

# ── wall ─────────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "wall" ]]; then
  FIRST=$(printf '%s' "$RESULT" | head -1 | cut -c1-60)
  emit_simple "${FIRST}"
  exit 0
fi

# ── mesg ─────────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "mesg" ]]; then
  emit_simple "$RESULT"
  exit 0
fi

# ── tty ──────────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "tty" ]]; then
  emit_simple "$RESULT"
  exit 0
fi

# ── biff (toggle) ────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "biff" ]]; then
  emit_simple "$RESULT"
  exit 0
fi

# ── talk ─────────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "talk" ]]; then
  FIRST=$(printf '%s' "$RESULT" | head -1 | cut -c1-60)
  emit_simple "${FIRST}"
  exit 0
fi

# ── talk_listen ──────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "talk_listen" ]]; then
  if [[ "$RESULT" == "No new messages. Still listening." ]]; then
    emit_simple "No new messages."
  else
    # Talk messages start with [HH:MM:SS] @user: body
    COUNT=$(printf '%s' "$RESULT" | grep -c '^\[')
    emit "${COUNT} new" "$RESULT"
  fi
  exit 0
fi

# ── talk_end ─────────────────────────────────────────────────────────
if [[ "$TOOL_NAME" == "talk_end" ]]; then
  emit_simple "talk ended"
  exit 0
fi

# ── Fallback: full output in panel ───────────────────────────────────
emit_simple "$RESULT"
