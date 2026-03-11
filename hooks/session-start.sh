#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$HOME/.claude/settings.json"
COMMANDS_DIR="$HOME/.claude/commands"
TOOL_PATTERN="mcp__plugin_biff_tty__"
DEV_TOOL_PATTERN="mcp__plugin_biff-dev_tty__"
STASH_PATH="$HOME/.biff/statusline-original.json"

ACTIONS=()

# ── Detect dev mode ──────────────────────────────────────────────────
# When plugin.json has "biff-dev", we're running as the dev plugin
# alongside the production "biff" plugin. Skip top-level command
# deployment — the prod plugin handles those.
IS_DEV=false
if command -v jq &>/dev/null && [[ -f "$PLUGIN_ROOT/.claude-plugin/plugin.json" ]]; then
  plugin_name="$(jq -r '.name // ""' "$PLUGIN_ROOT/.claude-plugin/plugin.json")"
  if [[ "$plugin_name" == *-dev ]]; then
    IS_DEV=true
  fi
fi

# ── Deploy top-level commands if missing ──────────────────────────────
# Skip *-dev.md files — dev commands use plugin namespace (biff-dev:who-dev)
# Skip entirely in dev mode — prod plugin deploys top-level commands
if [[ "$IS_DEV" == "false" ]]; then
  mkdir -p "$COMMANDS_DIR"
  DEPLOYED=()
  for cmd_file in "$PLUGIN_ROOT/commands/"*.md; do
    name="$(basename "$cmd_file")"
    [[ "$name" == *-dev.md ]] && continue
    dest="$COMMANDS_DIR/$name"
    if [[ ! -f "$dest" ]] || ! diff -q "$cmd_file" "$dest" >/dev/null 2>&1; then
      cp "$cmd_file" "$dest"
      DEPLOYED+=("/${name%.md}")
    fi
  done
  if [[ ${#DEPLOYED[@]} -gt 0 ]]; then
    ACTIONS+=("Deployed commands: ${DEPLOYED[*]}")
  fi
fi

# ── Allow MCP tools in user settings if not already allowed ──────────
if command -v jq &>/dev/null && [[ -f "$SETTINGS" ]]; then
  CHANGED=false

  # Allow prod tools
  if ! jq -e ".permissions.allow // [] | map(select(contains(\"$TOOL_PATTERN\"))) | length > 0" "$SETTINGS" >/dev/null 2>&1; then
    TMPFILE="$(mktemp)"
    jq '.permissions.allow = (.permissions.allow // []) + ["mcp__plugin_biff_tty__*"]' "$SETTINGS" > "$TMPFILE"
    mv "$TMPFILE" "$SETTINGS"
    CHANGED=true
  fi

  # Allow dev tools (only when running as biff-dev)
  if [[ "$IS_DEV" == "true" ]]; then
    if ! jq -e ".permissions.allow // [] | map(select(contains(\"$DEV_TOOL_PATTERN\"))) | length > 0" "$SETTINGS" >/dev/null 2>&1; then
      TMPFILE="$(mktemp)"
      jq '.permissions.allow = (.permissions.allow // []) + ["mcp__plugin_biff-dev_tty__*"]' "$SETTINGS" > "$TMPFILE"
      mv "$TMPFILE" "$SETTINGS"
      CHANGED=true
    fi
  fi

  if [[ "$CHANGED" == "true" ]]; then
    ACTIONS+=("Auto-allowed biff MCP tools in permissions")
  fi
fi

# ── Install statusline if not already active ─────────────────────────
if [[ ! -f "$STASH_PATH" ]]; then
  if command -v biff &>/dev/null; then
    biff install-statusline 2>/dev/null || true
    ACTIONS+=("Installed biff statusline (wraps existing)")
  fi
fi

# ── Notify Claude if anything was set up ─────────────────────────────
if [[ ${#ACTIONS[@]} -gt 0 ]]; then
  MSG="Biff plugin first-run setup complete."
  for action in "${ACTIONS[@]}"; do
    MSG="$MSG $action."
  done
  cat <<ENDJSON
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "$MSG"
  }
}
ENDJSON
fi

exit 0
