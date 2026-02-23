#!/usr/bin/env bash
# Tests for hooks/bead-claim.sh
#
# Run: bash tests/test_hooks/test_bead_claim.sh
# Requires: jq, git
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/bead-claim.sh"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ✗ $1: $2"; }

# ── Setup: temp git repo with .biff ──────────────────────────────────
TEST_REPO=$(mktemp -d)
trap 'rm -rf "$TEST_REPO"' EXIT

git -C "$TEST_REPO" init -q
touch "$TEST_REPO/.biff"

# Also create a repo WITHOUT .biff
TEST_REPO_NO_BIFF=$(mktemp -d)
trap 'rm -rf "$TEST_REPO" "$TEST_REPO_NO_BIFF"' EXIT
git -C "$TEST_REPO_NO_BIFF" init -q

echo "bead-claim.sh tests"
echo "────────────────────"

# ── Test 1: basic claim ──────────────────────────────────────────────
echo ""
echo "matching:"

OUTPUT=$(cd "$TEST_REPO" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "bd update biff-679 --status=in_progress"},
  "tool_response": "✓ biff-679 → in_progress"
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q '/plan'; then
  pass "basic claim suggests /plan"
else
  fail "basic claim suggests /plan" "got: $CTX"
fi

if echo "$OUTPUT" | jq -e '.hookSpecificOutput.updatedMCPToolOutput' >/dev/null 2>&1; then
  VALUE=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.updatedMCPToolOutput')
  if [[ "$VALUE" != "null" ]]; then
    fail "does not set updatedMCPToolOutput" "got: $VALUE"
  else
    pass "does not set updatedMCPToolOutput"
  fi
else
  pass "does not set updatedMCPToolOutput"
fi

# ── Test 2: different flag order ─────────────────────────────────────
OUTPUT=$(cd "$TEST_REPO" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "bd update --status=in_progress biff-679"},
  "tool_response": "✓ biff-679 → in_progress"
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q '/plan'; then
  pass "different flag order suggests /plan"
else
  fail "different flag order suggests /plan" "got: $CTX"
fi

# ── Test 3: chained command ──────────────────────────────────────────
OUTPUT=$(cd "$TEST_REPO" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "bd update biff-679 --status=in_progress && bd show biff-679"},
  "tool_response": "✓ biff-679 → in_progress\n..."
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q '/plan'; then
  pass "chained command suggests /plan"
else
  fail "chained command suggests /plan" "got: $CTX"
fi

# ── Test 4: --status in_progress (space instead of =) ────────────────
OUTPUT=$(cd "$TEST_REPO" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "bd update biff-679 --status in_progress"},
  "tool_response": "✓ biff-679 → in_progress"
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q '/plan'; then
  pass "space-separated --status in_progress suggests /plan"
else
  fail "space-separated --status in_progress suggests /plan" "got: $CTX"
fi

# ── Test 5: success gate — no ✓ in response ──────────────────────────
echo ""
echo "gating:"

OUTPUT=$(cd "$TEST_REPO" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "bd update biff-679 --status=in_progress"},
  "tool_response": "Error: issue not found"
}' | bash "$HOOK" 2>&1) || true

if [[ -z "$OUTPUT" ]]; then
  pass "silent exit when command failed (no ✓)"
else
  fail "silent exit when command failed (no ✓)" "got output: $OUTPUT"
fi

# ── Test 6: no .biff file → silent exit ──────────────────────────────
OUTPUT=$(cd "$TEST_REPO_NO_BIFF" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "bd update biff-679 --status=in_progress"},
  "tool_response": "✓ biff-679 → in_progress"
}' | bash "$HOOK" 2>&1) || true

if [[ -z "$OUTPUT" ]]; then
  pass "silent exit when no .biff file"
else
  fail "silent exit when no .biff file" "got output: $OUTPUT"
fi

# ── Test 7: unrelated Bash command → silent exit ─────────────────────
OUTPUT=$(cd "$TEST_REPO" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "git status"},
  "tool_response": "On branch main"
}' | bash "$HOOK" 2>&1) || true

if [[ -z "$OUTPUT" ]]; then
  pass "silent exit for unrelated Bash command"
else
  fail "silent exit for unrelated Bash command" "got output: $OUTPUT"
fi

# ── Test 8: bd update without in_progress → silent exit ──────────────
OUTPUT=$(cd "$TEST_REPO" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "bd update biff-679 --assignee=kai"},
  "tool_response": "✓ biff-679 updated"
}' | bash "$HOOK" 2>&1) || true

if [[ -z "$OUTPUT" ]]; then
  pass "silent exit for bd update without in_progress"
else
  fail "silent exit for bd update without in_progress" "got output: $OUTPUT"
fi

# ── Test 9: bd update --description containing in_progress → silent exit
OUTPUT=$(cd "$TEST_REPO" && echo '{
  "tool_name": "Bash",
  "tool_input": {"command": "bd update biff-679 --description=\"matches bd update.*in_progress pattern\""},
  "tool_response": "✓ biff-679 updated"
}' | bash "$HOOK" 2>&1) || true

if [[ -z "$OUTPUT" ]]; then
  pass "silent exit for --description containing in_progress"
else
  fail "silent exit for --description containing in_progress" "got output: $OUTPUT"
fi

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo "────────────────────"
TOTAL=$((PASS + FAIL))
echo "$PASS/$TOTAL passed"
[[ $FAIL -eq 0 ]] && echo "All tests passed." || { echo "$FAIL FAILED"; exit 1; }
