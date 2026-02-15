#!/usr/bin/env bash
# biff installer
# Usage: curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/main/install.sh | bash
#
# What this does:
#   1. Checks Python 3.13+ is available
#   2. Installs uv if not present
#   3. Installs biff-mcp via uv (the MCP server)
#   4. Registers the biff-commands plugin (slash commands for Claude Code)
#   5. Runs biff init (identity + team config)
#   6. Installs the status line

set -euo pipefail

REPO="https://github.com/punt-labs/biff.git"
PLUGIN_NAME="biff-commands"
PLUGIN_DIR_IN_REPO="plugins/biff-commands"
PLUGINS_DIR="$HOME/.claude/plugins/local-plugins/plugins"
MARKETPLACE="$HOME/.claude/plugins/local-plugins/.claude-plugin/marketplace.json"
INSTALL_DIR="$PLUGINS_DIR/$PLUGIN_NAME"
SETTINGS="$HOME/.claude/settings.json"
REGISTRY="$HOME/.claude/plugins/installed_plugins.json"

# --- Helpers ----------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()   { printf "${BLUE}%s${NC}\n" "$*"; }
ok()     { printf "  ${GREEN}✓${NC} %s\n" "$*"; }
warn()   { printf "  ${YELLOW}○${NC} %s\n" "$*"; }
fail()   { printf "  ${RED}✗${NC} %s\n" "$*"; exit 1; }
header() { printf "\n${BOLD}%s${NC}\n" "$*"; }

ask() {
  local prompt="$1"
  if [[ ! -t 0 ]]; then return 0; fi
  printf "  ${BLUE}%s [Y/n]${NC} " "$prompt"
  read -r answer </dev/tty
  [[ -z "$answer" || "$answer" =~ ^[Yy] ]]
}

# --- Step 1: Python ---------------------------------------------------------

header "Prerequisites"

if command -v python3 &>/dev/null; then
  PYTHON=python3
elif command -v python &>/dev/null; then
  PYTHON=python
else
  fail "Python not found. Install Python 3.13+ from https://python.org"
fi

PY_MAJOR=$($PYTHON -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PYTHON -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 13 ]; }; then
  fail "Python $PY_MAJOR.$PY_MINOR found, but 3.13+ is required"
fi

ok "Python $PY_MAJOR.$PY_MINOR"

if command -v git &>/dev/null; then
  ok "git"
else
  fail "git not found — install git first"
fi

if command -v claude &>/dev/null; then
  ok "claude CLI"
else
  warn "claude CLI not found in PATH"
fi

if command -v jq &>/dev/null; then
  ok "jq"
else
  warn "jq not found — marketplace registration will need manual steps"
fi

# --- Step 2: uv -------------------------------------------------------------

header "Package manager"

if command -v uv &>/dev/null; then
  ok "uv $(uv --version 2>&1 | head -1) already installed"
else
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if [ -f "$HOME/.local/bin/env" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.local/bin/env"
  elif [ -f "$HOME/.cargo/env" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.cargo/env"
  fi
  if command -v uv &>/dev/null; then
    ok "uv installed"
  else
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uv &>/dev/null; then
      ok "uv installed (added ~/.local/bin to PATH)"
    else
      fail "uv install succeeded but 'uv' not found on PATH. Restart your shell and re-run."
    fi
  fi
fi

# --- Step 3: biff-mcp (PyPI) ------------------------------------------------

header "MCP server"

INSTALL_OUTPUT=$(uv tool install biff-mcp 2>&1) || true
if echo "$INSTALL_OUTPUT" | grep -q "already installed"; then
  uv tool upgrade biff-mcp || fail "Failed to upgrade biff-mcp"
  ok "biff-mcp upgraded"
elif echo "$INSTALL_OUTPUT" | grep -q "Installed"; then
  ok "biff-mcp installed"
else
  echo "$INSTALL_OUTPUT"
  fail "Failed to install biff-mcp"
fi

if ! command -v biff &>/dev/null; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v biff &>/dev/null; then
    fail "biff-mcp installed but 'biff' not found on PATH. Run: export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
fi

ok "biff $(biff version 2>&1 || echo 'installed')"

# --- Step 4: biff-commands plugin -------------------------------------------

header "Plugin"

# Resolve the latest release tag
resolve_latest_tag() {
  git ls-remote --tags --sort=-v:refname "$REPO" 'v*' 2>/dev/null \
    | head -1 \
    | sed 's|.*refs/tags/||; s|\^{}||'
}

LATEST_TAG=$(resolve_latest_tag)

if [[ -d "$INSTALL_DIR" || -L "$INSTALL_DIR" ]]; then
  if [[ -L "$INSTALL_DIR" ]]; then
    ok "Symlink detected at $INSTALL_DIR (developer mode)"
  elif [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Existing installation found — updating..."
    git -C "$INSTALL_DIR" fetch --tags --quiet
    if [[ -n "$LATEST_TAG" ]]; then
      git -C "$INSTALL_DIR" checkout --quiet "$LATEST_TAG" 2>/dev/null
      ok "Updated to $LATEST_TAG"
    else
      git -C "$INSTALL_DIR" pull --quiet
      ok "Updated via git pull"
    fi
  else
    ok "Installed at $INSTALL_DIR"
  fi
else
  info "Cloning $REPO..."
  # Clone the full repo, then set up a sparse checkout for the plugin directory
  TMPDIR="$(mktemp -d)"
  git clone --quiet --depth 1 --filter=blob:none --sparse "$REPO" "$TMPDIR/biff"
  git -C "$TMPDIR/biff" sparse-checkout set "$PLUGIN_DIR_IN_REPO"
  if [[ -n "$LATEST_TAG" ]]; then
    git -C "$TMPDIR/biff" fetch --tags --depth 1 --quiet
    git -C "$TMPDIR/biff" checkout --quiet "$LATEST_TAG" 2>/dev/null || true
  fi

  mkdir -p "$PLUGINS_DIR"
  mv "$TMPDIR/biff/$PLUGIN_DIR_IN_REPO" "$INSTALL_DIR"
  rm -rf "$TMPDIR"

  if [[ -n "$LATEST_TAG" ]]; then
    ok "Installed $LATEST_TAG to $INSTALL_DIR"
  else
    ok "Installed to $INSTALL_DIR"
  fi
fi

# Read plugin metadata
PLUGIN_JSON="$INSTALL_DIR/.claude-plugin/plugin.json"
if [[ -f "$PLUGIN_JSON" ]] && command -v jq &>/dev/null; then
  PLUGIN_VERSION=$(jq -r '.version // "0.0.0"' "$PLUGIN_JSON")
  PLUGIN_DESCRIPTION=$(jq -r '.description // ""' "$PLUGIN_JSON")
  ok "Plugin version: $PLUGIN_VERSION"
else
  PLUGIN_VERSION="0.1.0"
  PLUGIN_DESCRIPTION="UNIX-style slash commands for biff team communication: /who, /finger, /plan, /mesg, /check, /biff"
  warn "Could not read plugin.json — using defaults"
fi

# --- Step 5: Registration ---------------------------------------------------

header "Registration"

DEFAULT_NAME="local"
DEFAULT_EMAIL="local@localhost"

if command -v git &>/dev/null; then
  GIT_NAME=$(git config user.name 2>/dev/null || true)
  GIT_EMAIL=$(git config user.email 2>/dev/null || true)
  [[ -n "$GIT_NAME" ]] && DEFAULT_NAME="$GIT_NAME"
  [[ -n "$GIT_EMAIL" ]] && DEFAULT_EMAIL="$GIT_EMAIL"
fi

if [[ -t 0 ]]; then
  printf "  ${BLUE}Author name [%s]:${NC} " "$DEFAULT_NAME"
  read -r AUTHOR_NAME </dev/tty
  [[ -z "$AUTHOR_NAME" ]] && AUTHOR_NAME="$DEFAULT_NAME"
  printf "  ${BLUE}Author email [%s]:${NC} " "$DEFAULT_EMAIL"
  read -r AUTHOR_EMAIL </dev/tty
  [[ -z "$AUTHOR_EMAIL" ]] && AUTHOR_EMAIL="$DEFAULT_EMAIL"
else
  AUTHOR_NAME="$DEFAULT_NAME"
  AUTHOR_EMAIL="$DEFAULT_EMAIL"
fi
ok "Author: $AUTHOR_NAME <$AUTHOR_EMAIL>"

# Create or update marketplace.json
MARKETPLACE_DIR="$(dirname "$MARKETPLACE")"
if [[ ! -f "$MARKETPLACE" ]]; then
  info "Creating marketplace.json..."
  mkdir -p "$MARKETPLACE_DIR"
  if command -v jq &>/dev/null; then
    jq -n \
      --arg name "$AUTHOR_NAME" \
      --arg email "$AUTHOR_EMAIL" \
      '{
        "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
        "name": "local",
        "description": "Local plugins",
        "owner": {"name": $name, "email": $email},
        "plugins": []
      }' > "$MARKETPLACE"
  else
    cat > "$MARKETPLACE" <<MANIFEST
{
  "\$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "local",
  "description": "Local plugins",
  "owner": {
    "name": "$AUTHOR_NAME",
    "email": "$AUTHOR_EMAIL"
  },
  "plugins": []
}
MANIFEST
  fi
  ok "Created $MARKETPLACE"
fi

if grep -q "\"$PLUGIN_NAME\"" "$MARKETPLACE" 2>/dev/null; then
  if command -v jq &>/dev/null; then
    CURRENT_VERSION=$(jq -r --arg name "$PLUGIN_NAME" \
      '.plugins[] | select(.name == $name) | .version' "$MARKETPLACE")
    if [[ "$CURRENT_VERSION" != "$PLUGIN_VERSION" ]]; then
      TMPFILE="$(mktemp)"
      jq --arg name "$PLUGIN_NAME" \
         --arg version "$PLUGIN_VERSION" \
         --arg desc "$PLUGIN_DESCRIPTION" \
         --arg author_name "$AUTHOR_NAME" \
         --arg author_email "$AUTHOR_EMAIL" \
         '(.plugins[] | select(.name == $name)) |= . + {
           "version": $version,
           "description": $desc,
           "author": {"name": $author_name, "email": $author_email}
         }' "$MARKETPLACE" > "$TMPFILE"
      mv "$TMPFILE" "$MARKETPLACE"
      ok "Updated marketplace entry: $CURRENT_VERSION → $PLUGIN_VERSION"
    else
      ok "Already registered (v$PLUGIN_VERSION)"
    fi
  else
    ok "Already registered in marketplace.json"
  fi
else
  if command -v jq &>/dev/null; then
    TMPFILE="$(mktemp)"
    jq --arg name "$PLUGIN_NAME" \
       --arg version "$PLUGIN_VERSION" \
       --arg desc "$PLUGIN_DESCRIPTION" \
       --arg author_name "$AUTHOR_NAME" \
       --arg author_email "$AUTHOR_EMAIL" \
       '.plugins += [{
         "name": $name,
         "description": $desc,
         "version": $version,
         "author": {"name": $author_name, "email": $author_email},
         "source": ("./plugins/" + $name),
         "category": "communication"
       }]' "$MARKETPLACE" > "$TMPFILE"
    mv "$TMPFILE" "$MARKETPLACE"
    ok "Registered in marketplace.json"
  else
    warn "jq not found — add the plugin entry to $MARKETPLACE manually"
  fi
fi

# Enable plugin in settings.json
PLUGIN_KEY="${PLUGIN_NAME}@local"
if [[ -f "$SETTINGS" ]] && command -v jq &>/dev/null; then
  if jq -e --arg key "$PLUGIN_KEY" '.enabledPlugins[$key]' "$SETTINGS" &>/dev/null; then
    ok "Plugin already enabled"
  else
    TMPFILE="$(mktemp)"
    jq --arg key "$PLUGIN_KEY" '.enabledPlugins[$key] = true' "$SETTINGS" > "$TMPFILE"
    mv "$TMPFILE" "$SETTINGS"
    ok "Enabled $PLUGIN_KEY in settings.json"
  fi
else
  warn "Could not update settings.json — enable '$PLUGIN_KEY' manually"
fi

# Clear plugin cache (rebuilt from source on next launch)
CACHE_DIR="$HOME/.claude/plugins/cache/local/$PLUGIN_NAME"
if [[ -d "$CACHE_DIR" ]]; then
  rm -rf "$CACHE_DIR"
  ok "Cleared plugin cache (will rebuild on next launch)"
fi

# --- Step 6: Status line ----------------------------------------------------

header "Status line"

biff install-statusline && ok "Status line installed" || warn "Status line setup skipped"

# --- Step 7: Init (if in a git repo) ----------------------------------------

header "Repo setup"

if git rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
  REPO_ROOT=$(git rev-parse --show-toplevel)
  if [[ -f "$REPO_ROOT/.biff" ]]; then
    ok ".biff already exists in $REPO_ROOT"
  else
    if ask "Run 'biff init' in $REPO_ROOT?"; then
      biff init --start "$REPO_ROOT"
    else
      info "  Run 'biff init' later to configure your team."
    fi
  fi
else
  info "Not in a git repo — run 'biff init' inside your project later."
fi

# --- Done --------------------------------------------------------------------

header "Done"
ok "biff is ready"
echo ""
info "Next steps:"
info "  1. Restart Claude Code (or start a new session)"
info "  2. Run 'biff init' in each project repo to configure teams"
info "  3. Use /who, /plan, /mesg, /finger, /check, /biff"
echo ""
