#!/usr/bin/env bash
set -euo pipefail

# Prepare plugin for release: swap name to prod, revert MCP server to prod
# command, remove -dev commands.  The tagged commit has only prod artifacts;
# the marketplace cache clones from it.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_JSON="${REPO_ROOT}/.claude-plugin/plugin.json"
COMMANDS_DIR="${REPO_ROOT}/commands"

# Preflight: abort if repo has uncommitted changes
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  echo "Error: repository has uncommitted changes. Commit or stash before running $(basename "$0")." >&2
  exit 1
fi

# Swap plugin name from *-dev to prod and revert MCP server to prod command
current_name="$(python3 -c "import json; print(json.load(open('${PLUGIN_JSON}'))['name'])")"
prod_name="${current_name%-dev}"

if [[ "$current_name" == "$prod_name" ]]; then
  echo "Plugin name is already '${prod_name}' (no -dev suffix)" >&2
  exit 1
fi

echo "Swapping plugin name: ${current_name} → ${prod_name}"
python3 -c "
import json, pathlib
p = pathlib.Path('${PLUGIN_JSON}')
d = json.loads(p.read_text())
d['name'] = '${prod_name}'
# Revert MCP server to prod launch command (installed CLI, not uv run)
tty = d.get('mcpServers', {}).get('tty')
if isinstance(tty, dict):
    tty['command'] = 'biff'
    tty['args'] = ['serve', '--transport', 'stdio']
p.write_text(json.dumps(d, indent=2) + '\n')
"

# Remove -dev commands
dev_files=()
while IFS= read -r -d '' f; do
  dev_files+=("$f")
done < <(find "$COMMANDS_DIR" -name '*-dev.md' -print0)

if [[ ${#dev_files[@]} -eq 0 ]]; then
  echo "No -dev commands found in ${COMMANDS_DIR}" >&2
  exit 1
fi

for f in "${dev_files[@]}"; do
  echo "Removing: $(basename "$f")"
done

git -C "$REPO_ROOT" add "$PLUGIN_JSON"
git -C "$REPO_ROOT" rm "${dev_files[@]}"
git -C "$REPO_ROOT" commit --no-verify -m "chore: prepare plugin for release [skip ci]"
