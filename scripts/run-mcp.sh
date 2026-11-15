#!/usr/bin/env bash
set -euo pipefail

export PATH="$PATH:$HOME/.local/bin:$HOME/.cargo/bin"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GOV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -x "$GOV_DIR/.venv/bin/python" ] && "$GOV_DIR/.venv/bin/python" --version >/dev/null 2>&1; then
  exec "$GOV_DIR/.venv/bin/python" "$GOV_DIR/mcp-server/server.py" "$@"
elif command -v uv >/dev/null 2>&1 && [ -f "$GOV_DIR/pyproject.toml" ]; then
  exec uv run --project "$GOV_DIR" python "$GOV_DIR/mcp-server/server.py" "$@"
else
  SITE_PKG="$(find "$GOV_DIR/.venv/lib" -maxdepth 2 -type d -name "site-packages" 2>/dev/null | head -n 1 || true)"
  if [ -n "$SITE_PKG" ]; then
    export PYTHONPATH="${SITE_PKG}${PYTHONPATH:+:$PYTHONPATH}"
  fi
  exec python3 "$GOV_DIR/mcp-server/server.py" "$@"
fi
