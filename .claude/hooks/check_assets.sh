#!/bin/bash
set -euo pipefail
# Resolve symlinks so invocation works both inside governance/ and a parent repo.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/../../scripts/check_assets.py"

if [ "$#" -eq 0 ]; then
  exec python3 "$PYTHON_SCRIPT" --mode claude
else
  exec python3 "$PYTHON_SCRIPT" "$@"
fi
