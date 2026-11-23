#!/usr/bin/env bash
# Print the name of the loaded plugin ("biff", or "biff-dev" under --plugin-dir),
# read from the plugin.json beside this hooks directory. The SessionStart
# dispatchers pass it to biff-hook so the warning names the right poll command.
# Prints nothing when the manifest cannot be read.
PLUGIN_JSON="$(dirname "$0")/../.claude-plugin/plugin.json"
sed -n 's/^[[:space:]]*"name":[[:space:]]*"\([^"]*\)".*/\1/p' "$PLUGIN_JSON" 2>/dev/null | sed -n 1p
exit 0
