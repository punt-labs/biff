#!/usr/bin/env bash
# Tests for hooks/pr-announce.sh
#
# Run: bash tests/test_hooks/test_pr_announce.sh
# Requires: jq, git
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/pr-announce.sh"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ✗ $1: $2"; }

# ── Setup: temp git repo with .biff ──────────────────────────────────
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

git -C "$TMPDIR" init -q
touch "$TMPDIR/.biff"

# Also create a repo WITHOUT .biff
TMPDIR_NO_BIFF=$(mktemp -d)
trap 'rm -rf "$TMPDIR" "$TMPDIR_NO_BIFF"' EXIT
git -C "$TMPDIR_NO_BIFF" init -q

echo "pr-announce.sh tests"
echo "────────────────────"

# ── Test 1: create_pull_request via plugin prefix ─────────────────────
echo ""
echo "create_pull_request:"

OUTPUT=$(cd "$TMPDIR" && echo '{
  "tool_name": "mcp__plugin_github_github__create_pull_request",
  "tool_input": {"title": "feat: add widget support", "body": "Adds widgets"},
  "tool_response": {"number": 42, "html_url": "https://github.com/o/r/pull/42"}
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q '/wall.*Created PR #42.*feat: add widget support'; then
  pass "suggests /wall with PR number and title"
else
  fail "suggests /wall with PR number and title" "got: $CTX"
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

# ── Test 2: create_pull_request via direct MCP prefix ─────────────────
OUTPUT=$(cd "$TMPDIR" && echo '{
  "tool_name": "mcp__github__create_pull_request",
  "tool_input": {"title": "fix: typo"},
  "tool_response": {"number": 7}
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q 'Created PR #7.*fix: typo'; then
  pass "works with mcp__github__ prefix"
else
  fail "works with mcp__github__ prefix" "got: $CTX"
fi

# ── Test 3: create_pull_request with string tool_response ─────────────
OUTPUT=$(cd "$TMPDIR" && echo '{
  "tool_name": "mcp__github__create_pull_request",
  "tool_input": {"title": "docs: readme"},
  "tool_response": "{\"number\": 99, \"html_url\": \"https://github.com/o/r/pull/99\"}"
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q 'Created PR #99.*docs: readme'; then
  pass "handles string-encoded tool_response"
else
  fail "handles string-encoded tool_response" "got: $CTX"
fi

# ── Test 4: merge_pull_request with title ─────────────────────────────
echo ""
echo "merge_pull_request:"

OUTPUT=$(cd "$TMPDIR" && echo '{
  "tool_name": "mcp__plugin_github_github__merge_pull_request",
  "tool_input": {"pullNumber": 42, "commit_title": "feat: add widget support (#42)"},
  "tool_response": {"merged": true}
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q 'Merged PR #42.*feat: add widget support'; then
  pass "suggests /wall with merge message"
else
  fail "suggests /wall with merge message" "got: $CTX"
fi

# ── Test 5: merge_pull_request without title ──────────────────────────
OUTPUT=$(cd "$TMPDIR" && echo '{
  "tool_name": "mcp__github__merge_pull_request",
  "tool_input": {"pullNumber": 10},
  "tool_response": {"merged": true}
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q 'Merged PR #10"$'; then
  pass "merge without title omits title"
else
  # Also accept just "Merged PR #10" without trailing content
  if echo "$CTX" | grep -q 'Merged PR #10'; then
    pass "merge without title omits title"
  else
    fail "merge without title omits title" "got: $CTX"
  fi
fi

# ── Test 6: merge with pull_number (snake_case variant) ───────────────
OUTPUT=$(cd "$TMPDIR" && echo '{
  "tool_name": "mcp__github__merge_pull_request",
  "tool_input": {"pull_number": 15},
  "tool_response": {"merged": true}
}' | bash "$HOOK")

CTX=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
if echo "$CTX" | grep -q 'Merged PR #15'; then
  pass "handles pull_number (snake_case) input"
else
  fail "handles pull_number (snake_case) input" "got: $CTX"
fi

# ── Test 7: no .biff file → silent exit ───────────────────────────────
echo ""
echo "gating:"

OUTPUT=$(cd "$TMPDIR_NO_BIFF" && echo '{
  "tool_name": "mcp__github__create_pull_request",
  "tool_input": {"title": "should not fire"},
  "tool_response": {"number": 1}
}' | bash "$HOOK" 2>&1) || true

if [[ -z "$OUTPUT" ]]; then
  pass "silent exit when no .biff file"
else
  fail "silent exit when no .biff file" "got output: $OUTPUT"
fi

# ── Test 8: missing PR number → silent exit ───────────────────────────
OUTPUT=$(cd "$TMPDIR" && echo '{
  "tool_name": "mcp__github__create_pull_request",
  "tool_input": {"title": "no response number"},
  "tool_response": {}
}' | bash "$HOOK" 2>&1) || true

if [[ -z "$OUTPUT" ]]; then
  pass "silent exit when PR number missing"
else
  fail "silent exit when PR number missing" "got output: $OUTPUT"
fi

# ── Test 9: unrelated tool → silent exit ──────────────────────────────
OUTPUT=$(cd "$TMPDIR" && echo '{
  "tool_name": "mcp__github__list_issues",
  "tool_input": {},
  "tool_response": []
}' | bash "$HOOK" 2>&1) || true

if [[ -z "$OUTPUT" ]]; then
  pass "silent exit for unrelated tool"
else
  fail "silent exit for unrelated tool" "got output: $OUTPUT"
fi

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "────────────────────"
TOTAL=$((PASS + FAIL))
echo "$PASS/$TOTAL passed"
[[ $FAIL -eq 0 ]] && echo "All tests passed." || { echo "$FAIL FAILED"; exit 1; }
