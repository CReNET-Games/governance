#!/usr/bin/env bash
# Crenet Games Governance Submodule Setup Script
# Links governance rules, skills, and tools into the parent workspace root and manages their lifecycle.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

# Uninstall / Clean mode
if [ "$1" == "--clean" ] || [ "$1" == "--uninstall" ]; then
  echo "Cleaning governance symlinks and .gitignore in: $PARENT_DIR"
  
  # Remove rules & skills symlinks
  rm -rf "$PARENT_DIR/.agents/rules/governance"
  if [ -d "$SCRIPT_DIR/.agents/skills" ]; then
    for skill_dir in "$SCRIPT_DIR/.agents/skills/"*; do
      if [ -d "$skill_dir" ]; then
        skill_name="$(basename "$skill_dir")"
        rm -rf "$PARENT_DIR/.agents/skills/$skill_name"
      fi
    done
  fi
  rm -f "$PARENT_DIR/.antigravity"
  echo "✔ Governance symlinks cleaned."

  # Clean .gitignore entries
  PARENT_GITIGNORE="$PARENT_DIR/.gitignore"
  if [ -f "$PARENT_GITIGNORE" ]; then
    tmp_gitignore=$(mktemp)
    grep -v "Crenet Governance" "$PARENT_GITIGNORE" | \
    grep -v "\.agents/rules/governance" | \
    grep -v "\.agents/skills/" > "$tmp_gitignore" || true
    mv "$tmp_gitignore" "$PARENT_GITIGNORE"
    echo "✔ Cleaned governance entries from parent .gitignore"
  fi

  # Clean AGENTS.md governance stub
  PARENT_AGENTS="$PARENT_DIR/AGENTS.md"
  if [ -f "$PARENT_AGENTS" ]; then
    tmp_agents=$(mktemp)
    grep -v "Crenet Governance Rules" "$PARENT_AGENTS" | \
    grep -v "Studio Governance Standards" | \
    grep -v "inherits global Crenet Games standards" | \
    grep -v "\.agents/rules/governance" > "$tmp_agents" || true
    mv "$tmp_agents" "$PARENT_AGENTS"
    echo "✔ Cleaned governance references from parent AGENTS.md"
  fi

  exit 0
fi

echo "Initializing Crenet Games Governance in parent workspace: $PARENT_DIR"

# Check if running as a submodule inside a parent repository
if [ "$SCRIPT_DIR" != "$PARENT_DIR" ]; then

  # 1. Remove legacy top-level root symlinks if present
  if [ -L "$PARENT_DIR/.agents" ]; then
    rm -f "$PARENT_DIR/.agents"
    echo "✔ Removed legacy root .agents symlink."
  fi
  if [ -L "$PARENT_DIR/.antigravity" ]; then
    rm -f "$PARENT_DIR/.antigravity"
    echo "✔ Removed legacy root .antigravity symlink."
  fi

  # Create physical .agents directory structure in parent project root
  mkdir -p "$PARENT_DIR/.agents/rules" "$PARENT_DIR/.agents/skills"

  # 2. Granularly link governance rules into parent's .agents/rules/governance
  rm -rf "$PARENT_DIR/.agents/rules/governance"
  ln -sfn "../../governance/.agents/rules" "$PARENT_DIR/.agents/rules/governance"
  echo "✔ Linked governance rules -> $PARENT_DIR/.agents/rules/governance"

  # 3. Granularly link each governance skill into parent's .agents/skills/
  for skill_dir in "$SCRIPT_DIR/.agents/skills/"*; do
    if [ -d "$skill_dir" ]; then
      skill_name="$(basename "$skill_dir")"
      rm -rf "$PARENT_DIR/.agents/skills/$skill_name"
      ln -sfn "../../governance/.agents/skills/$skill_name" "$PARENT_DIR/.agents/skills/$skill_name"
      echo "✔ Linked governance skill '$skill_name' -> $PARENT_DIR/.agents/skills/$skill_name"
    fi
  done

  # 4. Cleanup Orphan / Broken Symlinks to Governance
  if [ -d "$PARENT_DIR/.agents/rules" ]; then
    find "$PARENT_DIR/.agents/rules" -maxdepth 2 -type l | while read -r link; do
      if [ ! -e "$link" ]; then
        rm -f "$link"
        echo "✔ Cleaned orphan rule symlink: $(basename "$link")"
      fi
    done
  fi

  if [ -d "$PARENT_DIR/.agents/skills" ]; then
    find "$PARENT_DIR/.agents/skills" -maxdepth 2 -type l | while read -r link; do
      if [ ! -e "$link" ]; then
        rm -f "$link"
        echo "✔ Cleaned orphan skill symlink: $(basename "$link")"
      fi
    done
  fi

  # 5. Dynamic .gitignore Management & Pruning
  PARENT_GITIGNORE="$PARENT_DIR/.gitignore"
  touch "$PARENT_GITIGNORE"

  # Prune existing governance entries to rebuild clean list of active entries
  tmp_gi=$(mktemp)
  grep -v "Crenet Governance" "$PARENT_GITIGNORE" | \
  grep -v "\.agents/rules/governance" | \
  grep -v "\.agents/skills/" > "$tmp_gi" || true

  {
    cat "$tmp_gi"
    echo ""
    echo "# Crenet Governance Symlinks (Managed via ./governance/setup.sh)"
    echo ".agents/rules/governance"
    for skill_dir in "$SCRIPT_DIR/.agents/skills/"*; do
      if [ -d "$skill_dir" ]; then
        skill_name="$(basename "$skill_dir")"
        echo ".agents/skills/$skill_name"
      fi
    done
  } > "$PARENT_GITIGNORE"
  rm -f "$tmp_gi"
  echo "✔ Dynamically synchronized active governance symlinks in parent .gitignore"

  # 6. Handle parent repository AGENTS.md integration
  PARENT_AGENTS="$PARENT_DIR/AGENTS.md"
  GOV_IMPORT_STUB="<!-- Crenet Governance Rules -->"
  
  if [ -f "$PARENT_AGENTS" ]; then
    if ! grep -q "Crenet Governance Rules" "$PARENT_AGENTS"; then
      echo "" >> "$PARENT_AGENTS"
      echo "$GOV_IMPORT_STUB" >> "$PARENT_AGENTS"
      echo "## Studio Governance Standards" >> "$PARENT_AGENTS"
      echo "This project inherits global Crenet Games standards from the \`governance\` submodule:" >> "$PARENT_AGENTS"
      echo "- See [.agents/rules/governance/](.agents/rules/governance/) for active studio rules." >> "$PARENT_AGENTS"
      echo "✔ Appended governance rule index to existing parent AGENTS.md"
    fi
  else
    cat << 'EOF' > "$PARENT_AGENTS"
# Project Agent Guidelines

This repository uses Crenet Games governance standards.

## Studio Governance Standards
This project inherits global Crenet Games standards from the `governance` submodule:
- See [.agents/rules/governance/](.agents/rules/governance/) for active studio rules.

## Project-Specific Guidelines
Add game-specific directives and rules below.
EOF
    echo "✔ Created parent AGENTS.md referencing studio governance standards."
  fi
fi

echo "Governance setup complete!"
