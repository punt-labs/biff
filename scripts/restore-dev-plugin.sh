#!/usr/bin/env bash
set -euo pipefail

# Restore dev plugin state on main after a release tag.
#
# Usage:
#   scripts/restore-dev-plugin.sh [release-prep-commit]
#
# If no argument is given, auto-detects the last "prepare plugin for release"
# commit and restores from its parent.
#
# CONTRACT: this script stages the restored files but does NOT commit them.
# punt-kit's release Phase 9 re-stamps plugin.json's version (the restored
# dev commit's version field is stale) onto the same staged changes and
# commits the combined result itself. Committing here would leave nothing
# staged for punt-kit's commit, which then fails with "nothing to commit".
# The caller owns the commit's properties too: Phase 9's commit message
# already carries "[skip ci]" so the restore doesn't trigger a push-CI run,
# and deliberately omits --no-verify (org policy bans it) so local hooks
# still run against the restored files.

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

echo "Restoring dev state from release-prep commit ${RELEASE_PREP_COMMIT:0:12}"

# Swap the name back to -dev on the CURRENT (post-release-prep) plugin.json,
# rather than checking out the whole file from the parent commit. The parent
# predates release-plugin.sh's version bump too, so a whole-file checkout
# would silently revert the version along with the name -- exactly the drift
# this pair of scripts exists to prevent.
python3 -c "
import json, pathlib
p = pathlib.Path('${PLUGIN_JSON}')
d = json.loads(p.read_text())
if not d['name'].endswith('-dev'):
    d['name'] = d['name'] + '-dev'
p.write_text(json.dumps(d, indent=2) + '\n')
"

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

# The name-only edit above (unlike the old whole-file checkout) can be a
# genuine no-op -- e.g. re-running this script when the name is already
# -dev-suffixed and there were no dev commands to restore. Check explicitly
# and report it, since the caller (punt-kit Phase 9) commits separately and
# would otherwise see an empty diff with no explanation.
if git -C "$REPO_ROOT" diff --cached --quiet; then
  echo "Nothing to restore — plugin.json is already dev state and no dev commands were removed."
  exit 0
fi

echo "Staged dev plugin state restore (not committed — see CONTRACT above)."
