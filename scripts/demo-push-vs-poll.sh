#!/usr/bin/env bash
set -euo pipefail

# One-command demo: push beats poll for broadcast message detection
# (biff-5ex, DES-062). Runs the tier-3b comparison test against the real
# ghcr.io/punt-labs/biff-relay image and points the operator at the
# printed comparison table and the saved transcript.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TRANSCRIPT_FILE="${REPO_ROOT}/tests/transcripts/test_push_beats_poll_against_real_relay.txt"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not on PATH." >&2
  echo "This demo builds and runs the real biff-relay image, which needs Docker." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Error: the Docker daemon is not reachable (docker info failed)." >&2
  echo "Start Docker and re-run this script." >&2
  exit 1
fi

echo "Building biff-relay from docker/ and running the push-vs-poll demo..."
echo

cd "$REPO_ROOT"
uv run pytest -m nats_docker tests/test_relay_image/test_push_demo.py -v -s

echo
echo "Demo complete. The comparison table is above; the same run also"
echo "saved a human-readable transcript of the tool calls to:"
echo "  $TRANSCRIPT_FILE"
