#!/usr/bin/env bash
# Crenet Games Governance Setup Script
# - Per-repo mode (default): links rules, skills, hooks, and ignore symlinks
#   non-destructively into the parent workspace root.
# - Global mode (--global): installs a single shared crenet-governance MCP server
#   into $HOME and registers it cleanly in Antigravity, Cursor, and Claude Code.

set -euo pipefail

export PATH="$PATH:$HOME/.local/bin:$HOME/.cargo/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
GLOBAL_DIR="${CRENET_GOVERNANCE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/crenet-governance}"

AI_IGNORE_SYMLINKS=(
  ".geminiignore"
  ".aiexclude"
  ".cursorignore"
  ".claudeignore"
  ".clineignore"
  ".rooignore"
  ".aiderignore"
  ".codeiumignore"
  ".rgignore"
)

MODE="install"
GLOBAL_MODE=0
DISABLE_ASSET_PROVENANCE=0

for arg in "$@"; do
  case "$arg" in
    --global)
      GLOBAL_MODE=1
      ;;
    --clean|--uninstall)
      MODE="uninstall"
      ;;
    --no-asset-provenance)
      DISABLE_ASSET_PROVENANCE=1
      ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $0 [--global] [--clean|--uninstall] [--no-asset-provenance]" >&2
      exit 2
      ;;
  esac
done

if [ "$GLOBAL_MODE" -eq 1 ] && [ "$DISABLE_ASSET_PROVENANCE" -eq 1 ]; then
  echo "Error: --no-asset-provenance applies to per-repository setup, not --global." >&2
  exit 2
fi

update_global_mcp_configs() {
  local mode="$1"
  local launcher_path="$2"
  python3 - "$mode" "$launcher_path" "$HOME" <<'PY'
import json
import sys
from pathlib import Path

mode = sys.argv[1]
launcher_path = sys.argv[2]
home = Path(sys.argv[3])

targets = [
    ("Antigravity", home / ".gemini" / "config" / "mcp_config.json", {"command": launcher_path, "args": []}, True),
    ("Cursor", home / ".cursor" / "mcp.json", {"command": launcher_path, "args": []}, True),
    ("Claude Code", home / ".claude.json", {"type": "stdio", "command": launcher_path, "args": []}, False),
]

for label, cfg_path, entry, always_create in targets:
    if mode == "install":
        if not always_create and not cfg_path.exists() and not (home / ".claude").exists():
            continue
        if cfg_path.exists():
            raw = cfg_path.read_text(encoding="utf-8").strip()
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError(f"Expected JSON object in {cfg_path}")
        else:
            data = {}
        servers = data.get("mcpServers")
        if servers is None:
            servers = {}
        elif not isinstance(servers, dict):
            raise ValueError(f"Expected 'mcpServers' object in {cfg_path}")
        servers["crenet-governance"] = entry
        data["mcpServers"] = servers
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"✔ Registered crenet-governance MCP in {label} ({cfg_path})")
    elif mode == "uninstall":
        if not cfg_path.exists():
            continue
        raw = cfg_path.read_text(encoding="utf-8").strip()
        if not raw:
            continue
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {cfg_path}")
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and "crenet-governance" in servers:
            servers.pop("crenet-governance", None)
            data["mcpServers"] = servers
            cfg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(f"✔ Removed crenet-governance MCP from {label} ({cfg_path})")
PY
}

if [ "$GLOBAL_MODE" -eq 1 ]; then
  if [ "$MODE" = "uninstall" ]; then
    update_global_mcp_configs "uninstall" "$GLOBAL_DIR/scripts/run-mcp.sh"
    if [ -n "$GLOBAL_DIR" ] && [[ "$GLOBAL_DIR" == */crenet-governance ]]; then
      rm -rf "$GLOBAL_DIR"
    fi
    echo "✔ Global crenet-governance MCP server uninstalled."
    exit 0
  fi

  mkdir -p "$GLOBAL_DIR/mcp-server" "$GLOBAL_DIR/scripts" "$GLOBAL_DIR/schemas"
  cp "$SCRIPT_DIR/mcp-server/server.py" "$GLOBAL_DIR/mcp-server/server.py"
  cp "$SCRIPT_DIR/scripts/"*.py "$GLOBAL_DIR/scripts/"
  cp "$SCRIPT_DIR/scripts/run-mcp.sh" "$GLOBAL_DIR/scripts/run-mcp.sh"
  cp "$SCRIPT_DIR/schemas/asset_ledger_schema.json" "$GLOBAL_DIR/schemas/asset_ledger_schema.json"
  cp "$SCRIPT_DIR/fastmcp.json" "$GLOBAL_DIR/fastmcp.json"
  cp "$SCRIPT_DIR/pyproject.toml" "$GLOBAL_DIR/pyproject.toml"
  if [ -f "$SCRIPT_DIR/uv.lock" ]; then
    cp "$SCRIPT_DIR/uv.lock" "$GLOBAL_DIR/uv.lock"
  fi
  chmod +x "$GLOBAL_DIR/scripts/run-mcp.sh" "$GLOBAL_DIR/scripts/"*.py

  if [ "${CRENET_SKIP_UV_SYNC:-0}" != "1" ]; then
    if command -v uv >/dev/null 2>&1; then
      uv sync --frozen --no-dev --offline --project "$GLOBAL_DIR" >/dev/null 2>&1 \
        || uv sync --frozen --no-dev --project "$GLOBAL_DIR" >/dev/null 2>&1 \
        || true
    fi
    if [ ! -e "$GLOBAL_DIR/.venv" ] && [ -d "$SCRIPT_DIR/.venv" ]; then
      ln -s "$SCRIPT_DIR/.venv" "$GLOBAL_DIR/.venv"
    fi
  fi

  update_global_mcp_configs "install" "$GLOBAL_DIR/scripts/run-mcp.sh"
  echo "✔ Installed global crenet-governance MCP server -> $GLOBAL_DIR"
  exit 0
fi

if [ ! -e "$PARENT_DIR/.git" ]; then
  echo "Error: $PARENT_DIR is not a Git repository. Run ./governance/setup.sh from a parent repo, or ./setup.sh --global for global MCP setup." >&2
  exit 1
fi

update_fenced_gitignore() {
  local gi_path="$1"
  local mode="$2"
  shift 2
  python3 - "$gi_path" "$mode" "$@" <<'PY'
import sys
from pathlib import Path

gi_path = Path(sys.argv[1])
mode = sys.argv[2]
entries = sys.argv[3:]

START = "# >>> crenet-governance >>>"
END = "# <<< crenet-governance <<<"

legacy_exact = {
    "# Crenet Governance Symlinks (Managed via ./governance/setup.sh)",
    ".agents/rules/governance",
    ".agents/hooks.json",
    ".agents/plugins/crenet-governance",
    ".claude/",
    ".mcp.json",
    ".claude/hooks/check_assets.sh",
    ".claude/settings.json",
}

lines = gi_path.read_text(encoding="utf-8").splitlines() if gi_path.exists() else []
out: list[str] = []
inside_fence = False
for line in lines:
    stripped = line.strip()
    if stripped == START:
        inside_fence = True
        continue
    if stripped == END:
        inside_fence = False
        continue
    if inside_fence:
        continue
    if stripped in legacy_exact or stripped.startswith(".agents/skills/") or stripped.startswith(".claude/skills/") or stripped in {
        ".geminiignore", ".aiexclude", ".cursorignore", ".claudeignore",
        ".clineignore", ".rooignore", ".aiderignore", ".codeiumignore", ".rgignore",
    }:
        continue
    out.append(line)

while out and not out[-1].strip():
    out.pop()

if mode == "install" and entries:
    if out:
        out.append("")
    out.append(START)
    out.extend(entries)
    out.append(END)

gi_path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
PY
}

update_fenced_agents_md() {
  local agents_path="$1"
  local mode="$2"
  local rules_dir="$3"
  python3 - "$agents_path" "$mode" "$rules_dir" <<'PY'
import sys
from pathlib import Path

agents_path = Path(sys.argv[1])
mode = sys.argv[2]
rules_dir = Path(sys.argv[3])

START = "<!-- Crenet Governance Rules -->"
END = "<!-- /Crenet Governance Rules -->"

if not agents_path.exists():
    if mode == "uninstall":
        sys.exit(0)
    lines = [
        "# Project Agent Guidelines",
        "",
        "This repository uses Crenet Games governance standards.",
        "",
        "## Project-Specific Guidelines",
        "Add game-specific directives and rules below.",
    ]
    created = True
else:
    lines = agents_path.read_text(encoding="utf-8").splitlines()
    created = False

out: list[str] = []
inside_fence = False
inside_legacy = False
for line in lines:
    s = line.strip()
    if s == START:
        inside_fence = True
        inside_legacy = True
        continue
    if s == END:
        inside_fence = False
        inside_legacy = False
        continue
    if inside_fence and not inside_legacy:
        continue
    if inside_legacy:
        if (
            s == "## Studio Governance Standards"
            or "inherits global Crenet Games standards" in s
            or ".agents/rules/governance/" in s
            or not s
        ):
            continue
        inside_fence = False
        inside_legacy = False
    if (
        s == "## Studio Governance Standards"
        or "inherits global Crenet Games standards" in s
        or ".agents/rules/governance/" in s
    ):
        continue
    out.append(line)

while out and not out[-1].strip():
    out.pop()

if mode == "install":
    rule_files = sorted(rules_dir.glob("*.md")) if rules_dir.is_dir() else []
    block = [
        START,
        "## Studio Governance Standards",
        "This project inherits global Crenet Games standards from the `governance` submodule:",
    ]
    for rf in rule_files:
        block.append(f"- @[{rf.stem}](.agents/rules/governance/{rf.name})")
    block.append(END)
    if out:
        out.append("")
    out.extend(block)

agents_path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
print("created" if created else "updated")
PY
}

# Uninstall / Clean mode
if [ "$MODE" = "uninstall" ]; then
  echo "Cleaning governance symlinks and fenced entries in: $PARENT_DIR"

  rm -rf "$PARENT_DIR/.agents/rules/governance"
  if [ -d "$SCRIPT_DIR/.agents/skills" ]; then
    for skill_dir in "$SCRIPT_DIR/.agents/skills/"*; do
      if [ -d "$skill_dir" ]; then
        skill_name="$(basename "$skill_dir")"
        [ -L "$PARENT_DIR/.agents/skills/$skill_name" ] && rm -f "$PARENT_DIR/.agents/skills/$skill_name"
      fi
    done
  fi
  [ -L "$PARENT_DIR/.antigravity" ] && rm -f "$PARENT_DIR/.antigravity"
  [ -L "$PARENT_DIR/.agents/hooks.json" ] && rm -f "$PARENT_DIR/.agents/hooks.json"
  [ -L "$PARENT_DIR/.agents/plugins/crenet-governance" ] && rm -f "$PARENT_DIR/.agents/plugins/crenet-governance"
  rmdir "$PARENT_DIR/.agents/plugins" 2>/dev/null || true
  [ -L "$PARENT_DIR/.mcp.json" ] && rm -f "$PARENT_DIR/.mcp.json"

  if [ -L "$PARENT_DIR/.claude" ]; then
    rm -f "$PARENT_DIR/.claude"
  elif [ -d "$PARENT_DIR/.claude" ]; then
    [ -L "$PARENT_DIR/.claude/hooks/check_assets.sh" ] && rm -f "$PARENT_DIR/.claude/hooks/check_assets.sh"
    [ -L "$PARENT_DIR/.claude/settings.json" ] && rm -f "$PARENT_DIR/.claude/settings.json"
    if [ -d "$SCRIPT_DIR/.claude/skills" ]; then
      for skill_dir in "$SCRIPT_DIR/.claude/skills/"*; do
        if [ -e "$skill_dir" ]; then
          skill_name="$(basename "$skill_dir")"
          [ -L "$PARENT_DIR/.claude/skills/$skill_name" ] && rm -f "$PARENT_DIR/.claude/skills/$skill_name"
        fi
      done
    fi
  fi

  for ignore_link in "${AI_IGNORE_SYMLINKS[@]}"; do
    [ -L "$PARENT_DIR/$ignore_link" ] && rm -f "$PARENT_DIR/$ignore_link"
  done

  if [ -f "$PARENT_DIR/.gitignore" ]; then
    update_fenced_gitignore "$PARENT_DIR/.gitignore" "uninstall"
  fi
  if [ -f "$PARENT_DIR/AGENTS.md" ]; then
    update_fenced_agents_md "$PARENT_DIR/AGENTS.md" "uninstall" "$SCRIPT_DIR/.agents/rules" >/dev/null
  fi

  echo "✔ Governance symlinks cleaned."
  exit 0
fi

echo "Initializing Crenet Games Governance in parent workspace: $PARENT_DIR"

if [ "$SCRIPT_DIR" != "$PARENT_DIR" ]; then
  # 1. Remove legacy top-level symlinks safely (never rm -rf real dirs)
  if [ -L "$PARENT_DIR/.agents" ]; then
    rm -f "$PARENT_DIR/.agents"
    echo "✔ Removed legacy root .agents symlink."
  fi
  if [ -L "$PARENT_DIR/.antigravity" ]; then
    rm -f "$PARENT_DIR/.antigravity"
    echo "✔ Removed legacy root .antigravity symlink."
  fi
  if [ -L "$PARENT_DIR/.claude" ]; then
    rm -f "$PARENT_DIR/.claude"
    echo "✔ Replaced legacy .claude symlink with non-destructive .claude/ directory."
  fi

  # Create physical .agents and .claude directory layout in parent project root
  mkdir -p "$PARENT_DIR/.agents/rules" "$PARENT_DIR/.agents/skills"
  mkdir -p "$PARENT_DIR/.claude/hooks" "$PARENT_DIR/.claude/skills"

  # 2. Granularly link governance rules into .agents/rules/governance
  [ -L "$PARENT_DIR/.agents/rules/governance" ] && rm -f "$PARENT_DIR/.agents/rules/governance"
  ln -sfn "../../governance/.agents/rules" "$PARENT_DIR/.agents/rules/governance"
  echo "✔ Linked governance rules -> $PARENT_DIR/.agents/rules/governance"

  # 3. Granularly link each governance skill into .agents/skills/ and .claude/skills/
  GITIGNORE_ENTRIES=(
    ".agents/rules/governance"
    ".agents/hooks.json"
    ".claude/hooks/check_assets.sh"
  )

  for skill_dir in "$SCRIPT_DIR/.agents/skills/"*; do
    if [ -d "$skill_dir" ]; then
      skill_name="$(basename "$skill_dir")"
      rm -rf "$PARENT_DIR/.agents/skills/$skill_name"
      ln -sfn "../../governance/.agents/skills/$skill_name" "$PARENT_DIR/.agents/skills/$skill_name"
      GITIGNORE_ENTRIES+=(".agents/skills/$skill_name")

      if [ ! -e "$PARENT_DIR/.claude/skills/$skill_name" ] || [ -L "$PARENT_DIR/.claude/skills/$skill_name" ]; then
        rm -f "$PARENT_DIR/.claude/skills/$skill_name"
        ln -sfn "../../governance/.agents/skills/$skill_name" "$PARENT_DIR/.claude/skills/$skill_name"
        GITIGNORE_ENTRIES+=(".claude/skills/$skill_name")
      fi
      echo "✔ Linked governance skill '$skill_name'"
    fi
  done

  # 3b. Link Antigravity & Claude hooks non-destructively
  if [ -f "$SCRIPT_DIR/.agents/hooks.json" ]; then
    ln -sfn "../governance/.agents/hooks.json" "$PARENT_DIR/.agents/hooks.json"
    echo "✔ Linked agent hooks -> $PARENT_DIR/.agents/hooks.json"
  fi

  if [ -f "$SCRIPT_DIR/.claude/hooks/check_assets.sh" ]; then
    ln -sfn "../../governance/.claude/hooks/check_assets.sh" "$PARENT_DIR/.claude/hooks/check_assets.sh"
    echo "✔ Linked Claude hook runner -> $PARENT_DIR/.claude/hooks/check_assets.sh"
  fi

  if [ -f "$SCRIPT_DIR/.claude/settings.json" ]; then
    if [ ! -e "$PARENT_DIR/.claude/settings.json" ] || [ -L "$PARENT_DIR/.claude/settings.json" ]; then
      ln -sfn "../governance/.claude/settings.json" "$PARENT_DIR/.claude/settings.json"
      GITIGNORE_ENTRIES+=(".claude/settings.json")
      echo "✔ Linked Claude settings -> $PARENT_DIR/.claude/settings.json"
    else
      echo "⚠ Custom $PARENT_DIR/.claude/settings.json detected; left untouched."
    fi
  fi

  # 3c. Remove legacy per-repo MCP / workspace plugin symlinks (migrated to --global)
  [ -L "$PARENT_DIR/.agents/plugins/crenet-governance" ] && rm -f "$PARENT_DIR/.agents/plugins/crenet-governance"
  rmdir "$PARENT_DIR/.agents/plugins" 2>/dev/null || true
  [ -L "$PARENT_DIR/.mcp.json" ] && rm -f "$PARENT_DIR/.mcp.json"

  # 3d. Canonical .aiignore + Agent Ignore Symlinks
  if [ -f "$SCRIPT_DIR/.aiignore" ] && [ ! -f "$PARENT_DIR/.aiignore" ]; then
    cp "$SCRIPT_DIR/.aiignore" "$PARENT_DIR/.aiignore"
    echo "✔ Created canonical .aiignore in $PARENT_DIR/.aiignore"
  fi
  if [ -f "$PARENT_DIR/.aiignore" ]; then
    for ignore_link in "${AI_IGNORE_SYMLINKS[@]}"; do
      ln -sfn ".aiignore" "$PARENT_DIR/$ignore_link"
      GITIGNORE_ENTRIES+=("$ignore_link")
    done
    echo "✔ Linked agent ignore symlinks -> .aiignore"
  fi

  # 4. Cleanup orphan governance symlinks
  for dir_path in "$PARENT_DIR/.agents/rules" "$PARENT_DIR/.agents/skills" "$PARENT_DIR/.claude/skills"; do
    if [ -d "$dir_path" ]; then
      find "$dir_path" -maxdepth 2 -type l | while read -r link; do
        if [ ! -e "$link" ]; then
          rm -f "$link"
          echo "✔ Cleaned orphan symlink: $link"
        fi
      done
    fi
  done

  # 5. Fenced .gitignore synchronization
  update_fenced_gitignore "$PARENT_DIR/.gitignore" "install" "${GITIGNORE_ENTRIES[@]}"
  echo "✔ Synchronized fenced crenet-governance entries in $PARENT_DIR/.gitignore"

  # 6. Fenced AGENTS.md rule index synchronization
  agents_status="$(update_fenced_agents_md "$PARENT_DIR/AGENTS.md" "install" "$SCRIPT_DIR/.agents/rules")"
  echo "✔ Synchronized governance rule index ($agents_status) in $PARENT_DIR/AGENTS.md"
fi

# 7. Optional project-level asset provenance opt-out (.crenet-governance.json)
if [ "$DISABLE_ASSET_PROVENANCE" -eq 1 ]; then
  python3 - "$PARENT_DIR/.crenet-governance.json" <<'PY'
import json
import sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
data = {}
if cfg_path.exists():
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
data["asset_provenance"] = False
cfg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
  echo "✔ Set \"asset_provenance\": false in $PARENT_DIR/.crenet-governance.json"
fi

echo "Governance setup complete!"
