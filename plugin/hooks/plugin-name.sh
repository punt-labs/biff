#!/usr/bin/env bash
# Print the name of the loaded plugin ("biff", or "biff-dev" under --plugin-dir),
# the top-level "name" of the plugin.json beside this hooks directory. The
# SessionStart dispatchers pass it to biff-hook so the warning names the right
# poll command. Prints nothing when the manifest or python3 is unavailable.
PLUGIN_JSON="$(dirname "$0")/../.claude-plugin/plugin.json"
python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("name", ""))' "$PLUGIN_JSON" 2>/dev/null
exit 0
