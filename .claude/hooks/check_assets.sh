#!/usr/bin/env bash
# Claude Code Hook for Asset Ledger Enforcement
# Exits with status 1 if there are unlogged assets, preventing the hook from succeeding.

# Find the scripts directory relative to this script's physical location (resolving symlinks)
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/../../scripts/check_assets.py"

python3 "$PYTHON_SCRIPT" --mode claude
