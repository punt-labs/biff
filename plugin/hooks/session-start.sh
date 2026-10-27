#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMMANDS_DIR="$HOME/.claude/commands"

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

# Permission rules and the status line are not set up here: SessionStart
# never edits a settings file. `biff enable` adds biff's allow rules to the
# repo's .claude/settings.json; `biff install-statusline` installs the status
# line (punt-kit permissions.md §6, plugins.md).

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
