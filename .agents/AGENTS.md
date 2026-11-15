# Crenet Games Agent Governance & Rule Index

This file defines the primary agent rules and directives for all Crenet Games repositories.

## Active Rules (`.agents/rules/`)

- **[Code & Infrastructure Standards](rules/code-standards.md)**: Guidelines for HTML templates, semantic CSS, TypeScript POJO enums, Makefile orchestration, and Vite/Vitest monorepo & testing standards.
- **[Agent Execution & Search Speed Standards](rules/agent-execution-speed.md)**: Enforces parallel 800-line `read_file` batching, `.aiignore` symlink conventions, `git grep`/`rg` over raw `grep`, and fast inner-loop test/typecheck verification.

## Active Skills (`.agents/skills/` & `.claude/skills/`)

- **[atomic-commit-chain](skills/atomic-commit-chain/SKILL.md)**: Decomposes dirty post-exploration working trees into topologically ordered, `git bisect`-safe atomic commit chains with isolated worktree verification (`scripts/atomic_commit.py`) and a human-in-the-loop review gate.

## Active Hooks & Global MCP Server (`.agents/hooks.json`, `.claude/settings.json`, `mcp-server/server.py`)

- **Asset Provenance Enforcement (`scripts/check_assets.py`, `scripts/ledger.py`)**: Scans active repository assets and `artifacts/` intermediates, captures tool/prompt context mid-turn when assets are generated or modified, and enforces step-by-step provenance logging via the `crenet-governance` MCP server (`log_asset_provenance`, `move_asset_provenance`, `audit_asset_provenance`) writing `docs/assets_ledger.json`. Register once globally via `./setup.sh --global` (per-repo opt-out via `.crenet-governance.json` with `"asset_provenance": false`).
