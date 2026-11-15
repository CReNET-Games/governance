# AI Governance & Compliance Boilerplate for Indie Studios

[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2FCReNET-Games%2Fgovernance.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2FCReNET-Games%2Fgovernance?ref=badge_shield)

This repository is the canonical studio governance submodule for Crenet Games repositories. It centralizes engineering rules, agent workflow skills, asset provenance tooling, and a shared global MCP server for Antigravity, Cursor, and Claude Code.

---

## Architecture Overview

1. **Global FastMCP Governance Server (`mcp-server/server.py`, `scripts/run-mcp.sh`)**: Installed once into `~/.local/share/crenet-governance` via `--global` and registered across Antigravity (`~/.gemini/config/mcp_config.json`), Cursor (`~/.cursor/mcp.json`), and Claude Code (`~/.claude.json`). Exposes stateless, workspace-isolated tools for step-by-step asset provenance (`log_asset_provenance`, `move_asset_provenance`, `audit_asset_provenance`) and dependency-aware atomic commits (`atomic_commit_status`, `atomic_commit_hunks`, `atomic_commit_stage`, `atomic_commit_verify`).
2. **Recipe-Based Asset Provenance Ledger (`scripts/ledger.py`, `schemas/asset_ledger_schema.json`)**: Tracks every game asset and `artifacts/` intermediate using bounded `1..6` ordered `steps` (`human`, `ai`, or `procedural`), computing `ai_involvement` (`none` | `assisted` | `generated`), auto-hashing SHA-256 digests, tracking asset graduations (`previous_paths`), and generating Publisher / M&A Audit Scorecards (`ledger.py report`).
3. **Zero-Overhead Agent Hooks (`scripts/check_assets.py`)**: Uses tool-filtered `PostToolUse` checks (skipping benign read/test shell commands in `<1ms`) to capture intermediate subagent/MCP/CLI prompts into `PostInvocation` hints plus loop-guarded `Stop` safety checks for Antigravity, Cursor, and Claude Code.
4. **Non-Destructive Setup (`setup.sh`)**: Separates one-time global MCP server installation (`--global`) from per-repository submodule wiring (`.agents/`, `.claude/`, `.aiignore` symlinks, and fenced `.gitignore` / `AGENTS.md` sections) without overwriting project customizations. Opt a non-game repo out of asset tracking with `--no-asset-provenance` (`.crenet-governance.json`).

---

## Setup & Integration

### 1. One-Time Global MCP Server Install (`$HOME`)

Install a single shared `crenet-governance` MCP server to `~/.local/share/crenet-governance` and merge it safely into Antigravity, Cursor, and Claude Code user configs:

```bash
./setup.sh --global          # or ./governance/setup.sh --global
./setup.sh --global --clean  # remove global install and unregister MCP entries
```

### 2. Per-Repository Submodule Setup

Run `setup.sh` after adding or updating the `governance` submodule inside a game or service repository:

```bash
# Inside parent project root:
git submodule add <repo-url> governance
./governance/setup.sh

# Disable asset provenance enforcement for non-game web/docs repositories:
./governance/setup.sh --no-asset-provenance

# Remove governance symlinks and fenced entries:
./governance/setup.sh --clean
```

---

## Directory Structure

```text
governance/
├── .agents/
│   ├── AGENTS.md                                  # Studio governance rule & skill index
│   ├── hooks.json                                 # Agent-agnostic PostToolUse/PostInvocation/Stop hooks
│   ├── rules/
│   │   ├── agent-execution-speed.md               # Parallel reads, git grep, fast inner-loop verification
│   │   └── code-standards.md                      # HTML, CSS, TS POJO enums, Makefile, Vite/Vitest rules
│   └── skills/
│       └── atomic-commit-chain/                   # Stacked atomic commit & human review gate workflow
├── .claude/
│   ├── settings.json                              # Claude Code PostToolUse & Stop hooks
│   ├── hooks/check_assets.sh                      # POSIX hook wrapper delegating to check_assets.py
│   └── skills/atomic-commit-chain                 # Symlink to ../../.agents/skills/atomic-commit-chain
├── mcp-server/
│   └── server.py                                  # CrenetGovernance FastMCP server
├── schemas/
│   └── asset_ledger_schema.json                   # Draft-07 JSON Schema for v2 asset ledgers
├── scripts/
│   ├── atomic_commit.py                           # CLI & library for status/hunks/stage/verify
│   ├── check_assets.py                            # Hook runner for Antigravity, Cursor, and Claude Code
│   ├── ledger.py                                  # Asset ledger CLI (log, move, sync-moves, verify, report, migrate)
│   └── run-mcp.sh                                 # Portable MCP server launcher (PATH + .venv -> uv -> python3)
├── tests/
│   ├── test_atomic_commit.py                      # Tests for static graph, tsconfig aliases, worktree verify
│   ├── test_check_assets.py                       # Tests for tool-filtered hooks and loop protection
│   ├── test_evals_llm.py                          # Rubric/catalog tests and live LLM behavioral evals
│   ├── test_ledger.py                             # Tests for v2 schema, migration, graduation, scorecards
│   ├── test_server.py                             # Tests for CrenetGovernance FastMCP tools
│   ├── test_setup_script.py                       # Tests for non-destructive setup.sh local & --global lifecycle
│   └── test_skill_and_rule_lint.py                # Frontmatter, parity, executable mode, and link lint
├── fastmcp.json                                   # FastMCP server metadata
├── Makefile                                       # Offline test & eval orchestration
├── pyproject.toml                                 # Python dependencies & pytest configuration
└── setup.sh                                       # Non-destructive per-repo & --global setup script
```

---

## Asset Provenance CLI (`scripts/ledger.py`)

```bash
# Log a single-step human or procedural asset:
python3 governance/scripts/ledger.py log client/public/logo.svg --actor procedural --tool Inkscape --summary "Vector logo export."

# Graduate an approved artifact from artifacts/ into client/public/, preserving AI prompts:
python3 governance/scripts/ledger.py move artifacts/storefront/hero_raw.png client/public/hero.png \
  --actor human --tool GIMP --summary "Color graded and cropped to 1200x630."

# Verify disk assets against docs/assets_ledger.json and print a Publisher/M&A scorecard:
python3 governance/scripts/ledger.py verify
python3 governance/scripts/ledger.py report
```

---

## Testing & Verification

```bash
uv sync --frozen
make test          # Fast offline suite (-m "not llm_eval")
make lint-skills   # Skill/rule frontmatter, token budgets, and Markdown link lint
make test-evals    # Live LLM behavioral evaluations (requires provider API key)
```

## License

[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2FCReNET-Games%2Fgovernance.svg?type=large)](https://app.fossa.com/projects/git%2Bgithub.com%2FCReNET-Games%2Fgovernance?ref=badge_large)
