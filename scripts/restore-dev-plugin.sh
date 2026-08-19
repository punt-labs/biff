#!/usr/bin/env bash
set -euo pipefail

# Restore dev plugin state on main after a release tag.
#
# Usage:
#   scripts/restore-dev-plugin.sh [release-prep-commit]
#
# If no argument is given, auto-detects the last "prepare plugin for release"
# commit and restores from its parent.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_JSON="${REPO_ROOT}/plugin/.claude-plugin/plugin.json"

# Preflight: abort if repo has uncommitted changes
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -uno)" ]]; then
  echo "Error: repository has uncommitted changes. Commit or stash before running $(basename "$0")." >&2
  exit 1
fi

# Determine the release-prep commit to restore from
RELEASE_PREP_COMMIT="${1:-}"
if [[ -z "$RELEASE_PREP_COMMIT" ]]; then
  RELEASE_PREP_COMMIT="$(git -C "$REPO_ROOT" log -n 1 --grep='prepare plugin for release' --pretty=format:%H || true)"
  if [[ -z "$RELEASE_PREP_COMMIT" ]]; then
    echo "Error: could not find a 'prepare plugin for release' commit. Pass a commit or tag as the first argument." >&2
    exit 1
  fi
fi

echo "Restoring dev state from parent of ${RELEASE_PREP_COMMIT:0:12}"
git -C "$REPO_ROOT" checkout "${RELEASE_PREP_COMMIT}^" -- "$PLUGIN_JSON"

# Restore dev commands if the parent commit had a plugin/commands/ directory.
# The `add` belongs inside this branch, beside the checkout that populates the
# directory. It used to run unconditionally as
# `git add plugin/commands/ 2>/dev/null || true`, where the suppression existed
# to tolerate the no-commands case — but it also swallowed a genuine add
# failure in the case where commands WERE restored, and the commit below then
# shipped without them while reporting success.
if git -C "$REPO_ROOT" ls-tree "${RELEASE_PREP_COMMIT}^" -- plugin/commands/ | grep -q .; then
  git -C "$REPO_ROOT" checkout "${RELEASE_PREP_COMMIT}^" -- plugin/commands/
  git -C "$REPO_ROOT" add plugin/commands/
fi

git -C "$REPO_ROOT" add "$PLUGIN_JSON"
git -C "$REPO_ROOT" commit --no-verify -m "chore: restore dev plugin state [skip ci]"
