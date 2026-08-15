# AI Governance & Compliance Boilerplate for Indie Studios

This repository serves as a generalized, open-source governance framework designed for indie game developers working with AI Agents. It provides central legal compliance, IP protection, and an automated AI asset disclosure pipeline. It is built to be included as a Git submodule (e.g., under `governance/`) across multiple game repositories to enforce standardized rules for both human developers and autonomous AI Agents.

---

## Why this Architecture?

This boilerplate is designed to solve complex multi-agent orchestration and legal compliance challenges at scale. For engineering reviewers and recruiters, here is the architectural rationale:

1. **FastMCP (Model Context Protocol)**: By wrapping governance logic in a FastMCP server, we decouple compliance checks and ledgering from the core game engine. This creates a standardized, language-agnostic interface that any AI agent (using the Gemini API, Claude, etc.) can query to understand local environment rules and record its actions.
2. **JSON Ledgers for Authorship**: The Steam storefront requires strict disclosure for AI-generated assets. Instead of messy database dependencies, this architecture uses isolated JSON ledgers (`assets_ledger.json`) written directly to the active workspace. This provides a lightweight, immutable, and highly auditable paper trail for copyright defense and hybrid human-AI authorship tracking.
3. **Git Submodule Injection (DRY)**: To maintain a single source of truth for agent behavior (e.g., code standards, accessibility rules, VPN checks), this repository acts as a submodule. The included `setup.sh` installer dynamically symlinks specific rules and skills into the parent repository's `.agents/` folder. This ensures all active game projects instantly inherit updated governance rules without code duplication.

---

## Submodule Integration & Auto-Discovery

When cloned as a Git submodule under `governance/`, the AI agent framework automatically discovers and merges studio rules. Run `setup.sh` after adding or updating the submodule in your game repository:

```bash
# Inside parent project root:
git submodule add <repo-url> governance
./governance/setup.sh

# To clean or remove governance symlinks, .gitignore entries, and AGENTS.md references:
./governance/setup.sh --clean
```

### Symlink Lifecycle & `.gitignore` Management (`setup.sh`):

1. **Granular Merging**: Creates a physical `.agents/` folder in the parent repository, symlinking rules and skills into place.
2. **Automated `.gitignore` Synchronization**: Automatically adds symlinked governance paths to the parent's `.gitignore`. Running `setup.sh` dynamically prunes removed entries.
3. **Automated Teardown & Orphan Cleanup**: Removes broken symlinks and cleans up governance entries upon teardown.
4. **Parent `AGENTS.md` Integration**: Appends a studio governance index reference to the parent project's `AGENTS.md`.

---

## Directory Structure

```
governance/
├── .agents/                               # Canonical Agent Customization Directory
│   ├── AGENTS.md                          # Primary agent governance rule index
│   ├── rules/                             # Always-on domain & coding rules
│   │   ├── code-standards.md              # HTML, CSS, TypeScript POJO enums, Makefile & Jest rules
│   │   └── environment-isolation.md       # Strict environment isolation constraint check
│   └── skills/                            # On-demand procedural skill runbooks
├── mcp-server/
│   └── server.py                          # Central FastMCP Server
├── schemas/
│   └── asset_ledger_schema.json           # JSON Schema for asset ledger entries
├── tests/
│   ├── test_server.py                     # Pytest suite for MCP compliance tools
│   └── test_setup_script.py               # Pytest suite for setup.sh installer lifecycle
├── setup.sh                               # Submodule lifecycle installer
├── .env                                   # Governance environment configuration
├── fastmcp.json                           # FastMCP server configuration & tool metadata
├── pyproject.toml                         # Python project configuration & dependencies
└── README.md                              # Repository documentation & usage guide
```

---

## `uv` Setup Commands

Install dependencies, run unit tests, and launch the FastMCP server using `uv`:

```bash
# 1. Initialize project with uv (if setting up anew)
uv init --no-readme

# 2. Install required dependencies
uv add fastmcp pydantic-settings
uv add --dev pytest

# 3. Sync virtual environment
uv sync

# 4. Run the full unit test suite (MCP tools + setup.sh installer lifecycle)
uv run pytest -v

# 5. Run the MCP server natively
uv run mcp-server/server.py
```

---

## FastMCP Configuration (`fastmcp.json`)

The server configuration and tool metadata are declared in `fastmcp.json`:

```json
{
  "name": "GovernanceMCP",
  "version": "0.1.0",
  "description": "Centralized governance, compliance, and legal audit layer for game repositories.",
  "server": {
    "entrypoint": "mcp-server/server.py:mcp",
    "transport": "stdio"
  },
  "environment": {
    "ASSETS_LEDGER_PATH": "./docs/assets_ledger.json",
    "ALLOWED_EMAIL_DOMAINS": "@crenet.games",
    "RESTRICTED_NETWORK_PROFILES": "cisco,anyconnect,globalprotect,zscaler,pulse,forticlient"
  }
}
```

---

## MCP Server Tools (`mcp-server/server.py`)

The Model Context Protocol (MCP) server provides compliance tools to AI agents operating across local game repositories.

### 1. `log_ai_asset`
- **Purpose**: Maintains a strict legal paper trail for Steam AI disclosures, copyright defense, img2img pipelines, and human post-edit authorship tracking.
- **Dynamic Path Logic**: Writes directly to `./docs/assets_ledger.json` in the active workspace, keeping individual repository ledgers isolated.

### 2. `verify_environment_isolation`
- **Purpose**: Prevents IP contamination by verifying that the agent and environment comply with strict environment isolation rules.
- **Validation Logic**:
  - **Git Config Email**: Checks `git config user.email` in the local workspace. Allowed domains are configured via the `ALLOWED_EMAIL_DOMAINS` environment variable.
  - **Process / Network Detection**: Scans active processes (`ps aux`) for restricted network profiles configured via `RESTRICTED_NETWORK_PROFILES`.
  - **Action**: Raises a `RuntimeError` if unauthorized email domains or active restricted network profiles are detected, blocking unapproved commits or cloud deployments.

---

## Unit Testing

Run unit tests covering `log_ai_asset` workspace isolation, hybrid pipeline metadata, compliant/non-compliant git email validation, unauthorized network process detection, and `setup.sh` installer lifecycle:

```bash
uv run pytest -v
```
