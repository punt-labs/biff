#!/usr/bin/env bash
# Bootstrap script for biff.
# Usage: curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/main/install.sh | bash
set -euo pipefail

pip install biff-mcp
biff install
biff doctor
