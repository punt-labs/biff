#!/bin/sh
# Install biff — UNIX-style team communication for Claude Code.
# Usage: curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/main/install.sh | sh
set -eu

# --- Colors (disabled when not a terminal) ---
if [ -t 1 ]; then
  BOLD='\033[1m' GREEN='\033[32m' YELLOW='\033[33m' NC='\033[0m'
else
  BOLD='' GREEN='' YELLOW='' NC=''
fi

info() { printf '%b==>%b %s\n' "$BOLD" "$NC" "$1"; }
ok()   { printf '  %b✓%b %s\n' "$GREEN" "$NC" "$1"; }
fail() { printf '  %b✗%b %s\n' "$YELLOW" "$NC" "$1"; exit 1; }

PACKAGE="punt-biff"
BINARY="biff"

# --- Step 1: Python ---

info "Checking Python..."

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  fail "Python not found. Install Python 3.13+ from https://python.org"
fi

PY_MAJOR=$("$PYTHON" -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 13 ]; }; then
  fail "Python ${PY_MAJOR}.${PY_MINOR} found, but 3.13+ is required"
fi

ok "Python ${PY_MAJOR}.${PY_MINOR}"

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

# --- Step 3: Claude Code CLI ---

info "Checking Claude Code..."

if command -v claude >/dev/null 2>&1; then
  ok "claude CLI found"
else
  fail "'claude' CLI not found. Install Claude Code first: https://docs.anthropic.com/en/docs/claude-code"
fi

# --- Step 4: punt-biff ---

info "Installing $PACKAGE..."

INSTALL_OUTPUT="$(uv tool install "$PACKAGE" 2>&1)" || true
if printf '%s' "$INSTALL_OUTPUT" | grep -q "already installed"; then
  uv tool upgrade "$PACKAGE" || fail "Failed to upgrade $PACKAGE"
  ok "$PACKAGE upgraded"
elif printf '%s' "$INSTALL_OUTPUT" | grep -q "Installed"; then
  ok "$PACKAGE installed"
else
  printf '%s\n' "$INSTALL_OUTPUT"
  fail "Failed to install $PACKAGE"
fi

if ! command -v "$BINARY" >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v "$BINARY" >/dev/null 2>&1; then
    fail "$PACKAGE installed but '$BINARY' not found on PATH"
  fi
fi

ok "$BINARY $(command -v "$BINARY")"

# --- Step 5: biff install (marketplace + plugin) ---

info "Setting up Claude Code plugin..."
"$BINARY" install

# --- Step 6: biff doctor ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor
printf '\n'

# --- Done ---

printf '%b%b%s is ready!%b\n\n' "$GREEN" "$BOLD" "$PACKAGE" "$NC"
printf 'Restart Claude Code twice to activate:\n'
printf '  First restart  → SessionStart hook runs setup\n'
printf '  Second restart → slash commands active (/who, /write, etc.)\n\n'
