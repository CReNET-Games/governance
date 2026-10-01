# Crenet Games Agent Governance & Rule Index

This file defines the primary agent rules and directives for all Crenet Games repositories.

## Active Rules (`.agents/rules/`)

- **[Code & Infrastructure Standards](file:///media/efaj/data/workspace/governance/.agents/rules/code-standards.md)**: Guidelines for HTML templates, semantic CSS, TypeScript POJO enums, Makefile orchestration, and Jest tests.
- **[environment-isolation](file:///media/efaj/data/workspace/governance/.agents/rules/environment-isolation.md)**: Pre-commit strict environment isolation checks & Git config email validation.

## Active Skills (`.agents/skills/`)

None currently.

## Active Hooks (`.agents/hooks.json` & `.claude/hooks/`)

- **Asset Ledger Enforcement (`check_assets.py`)**: Automatically scans the workspace for newly created images and strictly enforces logging them into the `assets_ledger.json` via the `log_ai_asset` MCP tool. Applies to both Antigravity and Claude Code.
