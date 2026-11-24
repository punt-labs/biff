#!/usr/bin/env bash
# Install and exercise a release wheel without using the checkout or a relay.
# Usage: bash scripts/smoke-release.sh candidate VERSION WHEEL
#        bash scripts/smoke-release.sh published VERSION
set -euo pipefail

usage() {
  printf '%s\n' 'Usage: smoke-release.sh candidate VERSION WHEEL | published VERSION'
}

fail() {
  printf 'biff %s smoke failed (%s): %s\n' "$channel" "$version" "$*" >&2
  exit 1
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
channel="release"
version=${2:-}
case "${1:-}:$#" in
  candidate:3)
    channel="candidate wheel"
    wheel_dir=$(cd -- "$(dirname -- "$3")" && pwd -P) || fail "Cannot resolve the candidate wheel path"
    package="$wheel_dir/$(basename -- "$3")"
    [[ -f "$package" && "$package" == *.whl ]] || fail "Expected an existing wheel"
    ;;
  candidate:*)
    channel="candidate wheel"
    fail "Pass exactly one candidate wheel"
    ;;
  published:2)
    channel="production PyPI"
    package="punt-biff==$version"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
[[ "$version" =~ ^[0-9][0-9A-Za-z.!+_-]*$ ]] || fail "Expected a PEP 440 version without its v prefix"
printf 'Testing punt-biff %s from %s\n' "$version" "$channel"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
cd "$work"
uv_bin=$(command -v uv) || fail "uv is required"
clean=(env -i "PATH=$PATH" "HOME=$HOME")
"${clean[@]}" "$uv_bin" --no-config venv --quiet --python 3.13 venv
python="$work/venv/bin/python"
install=("${clean[@]}" "$uv_bin" --no-config pip install --quiet --python "$python" --default-index https://pypi.org/simple/)
if [[ "$channel" == "production PyPI" ]]; then
  for attempt in {1..5}; do
    if "${install[@]}" --refresh-package punt-biff --only-binary punt-biff "$package" packaging; then
      break
    fi
    [[ "$attempt" != 5 ]] || fail "Could not install the tagged version from PyPI"
    sleep 15
  done
else
  "${install[@]}" "$package" packaging
fi

# A fresh HOME and git repo with no relay configured, so the CLI and the MCP
# server resolve the same default relay: the local relay, with no network.
mkdir "$work/home" "$work/repo"
isolated=(env -i "PATH=$work/venv/bin:/usr/bin:/bin" "HOME=$work/home" "PUNT_TELEMETRY=off"
  "GIT_CONFIG_GLOBAL=/dev/null" "GIT_CONFIG_SYSTEM=/dev/null")
cd "$work/repo"
"${isolated[@]}" git init --quiet
"${isolated[@]}" git remote add origin https://github.com/punt-labs/biff-release-smoke.git
biff=("${isolated[@]}" "$work/venv/bin/biff")

actual=$("${biff[@]}" version) || fail "Installed biff version command failed"
actual=${actual#biff }
"$python" -I -c 'import sys; from packaging.version import Version; sys.exit(Version(sys.argv[1]) != Version(sys.argv[2]))' \
  "$actual" "$version" || fail "Expected installed CLI version $version, got $actual"

cli_body="The release canary lives in the blue observatory."
"${biff[@]}" --user alice write bob "$cli_body" >/dev/null || fail "Installed CLI could not send a message"
"${biff[@]}" --json --user bob read >"$work/inbox.json" || fail "Installed CLI could not read the inbox"
"$python" -I - "$work/inbox.json" "$cli_body" <<'PY' || fail "CLI read did not return the message alice sent"
import json
import sys
from pathlib import Path

path, body = sys.argv[1:]
messages = json.loads(Path(path).read_text())
sys.exit(0 if any(m["from_user"] == "alice" and m["body"] == body for m in messages) else 1)
PY
printf 'PASS: punt-biff %s from %s (installed CLI write → read)\n' "$version" "$channel"

"${biff[@]}" enable >/dev/null || fail "Installed CLI could not enable biff in the repo"
mcp_body="The MCP canary lives in the red lighthouse."
"${biff[@]}" --user alice write bob "$mcp_body" >/dev/null || fail "Installed CLI could not send the MCP message"
"${isolated[@]}" "$python" -I - "$work/venv/bin/biff" "$mcp_body" <<'PY' ||
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

biff, body = sys.argv[1:]


async def main() -> None:
    server = StdioServerParameters(command=biff, args=["mcp", "--user", "bob"], env=dict(os.environ))
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        if "read_messages" not in names:
            sys.exit(f"MCP server did not list read_messages: {sorted(names)}")
        result = await session.call_tool("read_messages", {})
        text = "".join(getattr(part, "text", "") for part in result.content)
        if result.is_error or body not in text:
            sys.exit(f"MCP read_messages did not return the message: {text!r}")


asyncio.run(asyncio.wait_for(main(), timeout=60))
PY
  fail "Installed MCP server could not read the message over stdio"
printf 'PASS: punt-biff %s from %s (installed MCP stdio read_messages)\n' "$version" "$channel"
