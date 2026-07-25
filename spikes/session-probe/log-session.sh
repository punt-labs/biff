#!/usr/bin/env bash
#
# log-session.sh — SessionStart probe for biff-7ak.
#
# Answers the crux question the tty-resume fix hinges on: is Claude Code's
# session_id STABLE across `claude --resume`? SessionStart fires on every
# startup AND resume, with `source` distinguishing them and `session_id` in the
# hook JSON on stdin. This appends one line per session start to
# .tmp/session-probe.log so a resume can be compared to the original startup.
#
# Read the truth: after a /exit + `claude --resume`, look at
# .tmp/session-probe.log — if the `source=resume` line's session_id matches the
# prior `source=startup` line's, the id is stable and Option B is buildable.
#
# Remove: delete the "SessionStart" block from .claude/settings.local.json.
set -euo pipefail

payload="$(cat)"
dir="${CLAUDE_PROJECT_DIR:-.}"
mkdir -p "$dir/.tmp"
python3 - "$payload" "$dir/.tmp/session-probe.log" <<'PY'
import sys, json, datetime
raw, log = sys.argv[1], sys.argv[2]
try:
    d = json.loads(raw or "{}")
except Exception:
    d = {}
line = (
    f'{datetime.datetime.now().isoformat()}  '
    f'source={d.get("source")}  '
    f'session_id={d.get("session_id")}  '
    f'cwd={d.get("cwd")}  '
    f'transcript={d.get("transcript_path")}\n'
)
with open(log, "a") as fh:
    fh.write(line)
PY
