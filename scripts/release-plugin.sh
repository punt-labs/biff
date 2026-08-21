#!/usr/bin/env bash
set -euo pipefail

# Prepare plugin for release: swap name to prod, remove -dev commands.
# The tagged commit has only prod artifacts; the marketplace cache clones
# from it.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_JSON="${REPO_ROOT}/plugin/.claude-plugin/plugin.json"
PYPROJECT="${REPO_ROOT}/pyproject.toml"
COMMANDS_DIR="${REPO_ROOT}/plugin/commands"

# Preflight: the commands directory must exist. The `find` below runs inside a
# process substitution, so `set -e` does not see its failure — a missing or
# wrong COMMANDS_DIR would silently yield zero `*-dev.md` matches, take the
# "name swap only" branch, and ship a release-prep commit that never stripped
# the dev commands.
if [[ ! -d "$COMMANDS_DIR" ]]; then
  echo "Error: commands directory not found: $COMMANDS_DIR" >&2
  exit 1
fi

# Preflight: abort if repo has uncommitted changes
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -uno)" ]]; then
  echo "Error: repository has uncommitted changes. Commit or stash before running $(basename "$0")." >&2
  exit 1
fi

# Swap plugin name from *-dev to prod
current_name="$(python3 -c "import json; print(json.load(open('${PLUGIN_JSON}'))['name'])")"
prod_name="${current_name%-dev}"

if [[ "$current_name" == "$prod_name" ]]; then
  echo "Plugin name is already '${prod_name}' (no -dev suffix)" >&2
  exit 1
fi

echo "Swapping plugin name: ${current_name} → ${prod_name}"

# plugin.json's version has its own field, independent of pyproject.toml's,
# and nothing else in this pipeline keeps them in sync -- without this, the
# name swap below is the only edit this script makes, and version drifts
# silently release after release (confirmed stuck at 1.13.0 through two
# later releases before this fix). pyproject.toml is the source of truth by
# this point in the pipeline: phase 2's version-bump commit has already
# merged to main before release-prep runs -- but that's an ordering
# invariant enforced elsewhere, not by this script, so the sync below
# validates the extracted value is well-formed and moves strictly forward
# rather than trusting it blindly (review finding: a malformed or stale
# pyproject.toml version would otherwise ship silently to the marketplace
# manifest with only an easy-to-miss log line as a trace).
pyproject_version="$(python3 -c "
import json, pathlib, tomllib

pyproject = tomllib.loads(pathlib.Path('${PYPROJECT}').read_text())
new_version = pyproject['project']['version']

plugin_path = pathlib.Path('${PLUGIN_JSON}')
d = json.loads(plugin_path.read_text())
old_version = d.get('version', '0.0.0')

def _tup(v):
    parts = v.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f\"version '{v}' is not X.Y.Z semver\")
    return tuple(int(p) for p in parts)

if _tup(new_version) <= _tup(old_version):
    raise SystemExit(
        f'refusing to sync backwards/no-op plugin.json version: '
        f'{old_version} -> {new_version}'
    )

d['name'] = '${prod_name}'
d['version'] = new_version
plugin_path.write_text(json.dumps(d, indent=2) + '\n')
print(new_version)
")"
echo "Synced plugin.json version to pyproject.toml: ${pyproject_version}"

# Remove -dev commands
dev_files=()
while IFS= read -r -d '' f; do
  dev_files+=("$f")
done < <(find "$COMMANDS_DIR" -name '*-dev.md' -print0)

if [[ ${#dev_files[@]} -eq 0 ]]; then
  echo "No -dev commands found — name swap only"
else
  for f in "${dev_files[@]}"; do
    echo "Removing: $(basename "$f")"
  done
  git -C "$REPO_ROOT" rm "${dev_files[@]}"
fi

git -C "$REPO_ROOT" add "$PLUGIN_JSON"
git -C "$REPO_ROOT" commit --no-verify -m "chore: prepare plugin for release"
