#!/bin/sh
# Install biff — UNIX-style team communication for Claude Code.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/<SHA>/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --no-plugin   # CLI only, skip the plugin
set -eu

usage() {
  cat <<'EOF'
install.sh — install biff (CLI + Claude Code plugin)

Usage:
  curl -fsSL .../install.sh | sh                    # install CLI and plugin
  curl -fsSL .../install.sh | sh -s -- --no-plugin  # install CLI only
  curl -fsSL .../install.sh | BIFF_NO_PLUGIN=1 sh   # install CLI only (env)

Options:
  --no-plugin   Install the biff CLI but skip the Claude Code plugin.
  -h, --help    Show this help and exit.

Environment:
  BIFF_NO_PLUGIN=1   Same effect as --no-plugin (only the literal 1 is honored).
EOF
}

# --- Argument parsing (before any work) ---

SKIP_PLUGIN_REQUESTED=0
for arg in "$@"; do
  case "$arg" in
    --no-plugin) SKIP_PLUGIN_REQUESTED=1 ;;
    -h|--help)   usage; exit 0 ;;
    *)           printf 'install.sh: unknown option: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "${BIFF_NO_PLUGIN:-}" = "1" ]; then
  SKIP_PLUGIN_REQUESTED=1
fi

# --- Colors (disabled when not a terminal) ---
if [ -t 1 ]; then
  BOLD='\033[1m' GREEN='\033[32m' YELLOW='\033[33m' NC='\033[0m'
else
  BOLD='' GREEN='' YELLOW='' NC=''
fi

info() { printf '%b▶%b %s\n' "$BOLD" "$NC" "$1"; }
ok()   { printf '  %b✓%b %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '  %b!%b %s\n' "$YELLOW" "$NC" "$1"; }
fail() { printf '  %b✗%b %s\n' "$YELLOW" "$NC" "$1"; exit 1; }

VERSION="1.12.2"
MARKETPLACE_REPO="punt-labs/claude-plugins"
MARKETPLACE_NAME="punt-labs"
PLUGIN_NAME="biff"
PACKAGE="punt-biff"
BINARY="biff"

# --- Step 1: Prerequisites ---
#
# claude and git are required only for the Claude Code plugin. Their absence
# does not block the CLI — it auto-skips the plugin (install-cli-only.md).

info "Checking prerequisites..."

HAVE_CLAUDE=0
if command -v claude >/dev/null 2>&1; then
  ok "claude CLI found"
  HAVE_CLAUDE=1
else
  warn "'claude' CLI not found — installing biff CLI only (plugin skipped)"
fi

HAVE_GIT=0
if command -v git >/dev/null 2>&1; then
  ok "git found"
  HAVE_GIT=1
else
  warn "'git' not found — installing biff CLI only (plugin skipped)"
fi

# A single boolean gates the plugin steps: explicit request, env var, or a
# missing capability. There is no counter-flag to force the plugin on.
SKIP_PLUGIN=0
if [ "$SKIP_PLUGIN_REQUESTED" = "1" ] || [ "$HAVE_CLAUDE" = "0" ] || [ "$HAVE_GIT" = "0" ]; then
  SKIP_PLUGIN=1
fi

# --- Step 2: uv ---

info "Checking uv..."

if command -v uv >/dev/null 2>&1; then
  ok "uv already installed"
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
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    fail "uv install succeeded but 'uv' not found on PATH. Restart your shell and re-run."
  fi
  ok "uv installed"
fi

# --- Step 3: Python 3.13+ ---

info "Checking Python..."

PYTHON_FLAG=""
HAVE_PYTHON=0
if command -v python3 >/dev/null 2>&1; then
  PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
  PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
  if [ "$PY_MAJOR" -gt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 13 ]; }; then
    ok "Python ${PY_MAJOR}.${PY_MINOR}"
    HAVE_PYTHON=1
  fi
fi

if [ "$HAVE_PYTHON" = "0" ]; then
  info "Installing Python 3.13 via uv..."
  uv python install 3.13 || fail "Failed to install Python 3.13"
  ok "Python 3.13 (uv-managed)"
  PYTHON_FLAG="--python 3.13"
fi

# --- Step 4: Install biff CLI ---

info "Installing $PACKAGE..."

# shellcheck disable=SC2086
uv tool install --force $PYTHON_FLAG "$PACKAGE==$VERSION" || fail "Failed to install $PACKAGE==$VERSION"
ok "$PACKAGE installed"

if ! command -v "$BINARY" >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v "$BINARY" >/dev/null 2>&1; then
    fail "$PACKAGE installed but '$BINARY' not found on PATH"
  fi
fi

ok "$BINARY $(command -v "$BINARY")"

# --- Steps 4-6: Claude Code plugin (skipped in CLI-only mode) ---

if [ "$SKIP_PLUGIN" = "0" ]; then
  # --- Step 4: Register marketplace ---

  info "Registering Punt Labs marketplace..."

  if claude plugin marketplace list < /dev/null 2>/dev/null | grep -q "$MARKETPLACE_NAME"; then
    ok "marketplace already registered"
    claude plugin marketplace update "$MARKETPLACE_NAME" < /dev/null 2>/dev/null || true
  else
    claude plugin marketplace add "$MARKETPLACE_REPO" < /dev/null || fail "Failed to register marketplace"
    ok "marketplace registered"
  fi

  # --- Step 5: SSH fallback for plugin install ---

  # claude plugin install clones via SSH (git@github.com:...).
  # Users without SSH keys need an HTTPS fallback.
  NEED_HTTPS_REWRITE=0
  cleanup_https_rewrite() {
    if [ "$NEED_HTTPS_REWRITE" = "1" ]; then
      git config --global --unset url."https://github.com/".insteadOf 2>/dev/null || true
      NEED_HTTPS_REWRITE=0
    fi
  }
  trap cleanup_https_rewrite EXIT INT TERM

  if ! ssh -n -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    warn "SSH auth to GitHub unavailable, using HTTPS fallback"
    git config --global url."https://github.com/".insteadOf "git@github.com:"
    NEED_HTTPS_REWRITE=1
  fi

  # --- Step 6: Install plugin ---

  info "Installing $PLUGIN_NAME plugin..."

  claude plugin uninstall "${PLUGIN_NAME}@${MARKETPLACE_NAME}" < /dev/null 2>/dev/null || true
  if ! claude plugin install "${PLUGIN_NAME}@${MARKETPLACE_NAME}" < /dev/null; then
    cleanup_https_rewrite
    fail "Failed to install $PLUGIN_NAME"
  fi
  if ! claude plugin list < /dev/null 2>/dev/null | grep -q "$PLUGIN_NAME@$MARKETPLACE_NAME"; then
    cleanup_https_rewrite
    fail "$PLUGIN_NAME install reported success but plugin not found"
  fi
  ok "$PLUGIN_NAME plugin installed"

  cleanup_https_rewrite
fi

# --- Step 7: Verify ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor || true
printf '\n'

# --- Done ---

if [ "$SKIP_PLUGIN" = "1" ]; then
  printf '%b%bbiff CLI is ready!%b\n\n' "$GREEN" "$BOLD" "$NC"
  printf 'The biff CLI and MCP server are installed. Next steps:\n'
  printf '  biff doctor   # re-check installation health\n'
  printf '  biff install  # deploy this clone'\''s git hooks + user-scope agent guide\n'
  printf '  biff enable   # enable biff in a git repo (writes the committed marker + CI workflow)\n'
  printf '  biff mcp      # stdio MCP server to register with your harness\n\n'
  printf 'To add the Claude Code plugin later:\n'
  printf '  claude plugin marketplace add %s\n' "$MARKETPLACE_REPO"
  printf '  claude plugin install %s@%s\n\n' "$PLUGIN_NAME" "$MARKETPLACE_NAME"
else
  printf '%b%b%s is ready!%b\n\n' "$GREEN" "$BOLD" "$PLUGIN_NAME" "$NC"
  printf 'Restart Claude Code twice to activate:\n'
  printf '  First restart  → SessionStart hook runs setup\n'
  printf '  Second restart → slash commands active (/who, /write, etc.)\n\n'
fi
