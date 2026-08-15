"""Crenet Games Core Standards MCP Server.

Provides centralized governance, compliance checks, and legal paper trail logging
for human developers and AI Agents operating across Crenet Games repositories.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import List, Optional

try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

# Instantiate the central Model Context Protocol server for Crenet Games
mcp = FastMCP("CrenetGovernance")

# Approved compliant email domains
_ALLOWED_DOMAINS_ENV = os.environ.get("ALLOWED_EMAIL_DOMAINS", "@crenet.games")
APPROVED_EMAIL_DOMAINS = tuple(domain.strip().lower() for domain in _ALLOWED_DOMAINS_ENV.split(",") if domain.strip())

# Restricted network indicators to flag in process scans
_RESTRICTED_NETWORK_ENV = os.environ.get("RESTRICTED_NETWORK_PROFILES", "cisco,anyconnect,globalprotect,zscaler,pulse,forticlient")
RESTRICTED_NETWORK_KEYWORDS = tuple(kw.strip().lower() for kw in _RESTRICTED_NETWORK_ENV.split(",") if kw.strip())

# Allowed generation pipeline methodologies
VALID_GENERATION_TYPES = {
    "pure_ai",
    "sketch_to_ai",
    "ai_to_post_edit",
    "sketch_to_ai_to_post_edit",
}


@mcp.tool()
def log_ai_asset(
    file_name: str,
    generation_type: str = "sketch_to_ai_to_post_edit",
    base_human_sketch_ref: Optional[str] = None,
    ai_prompt_used: Optional[str] = None,
    ai_model_used: Optional[str] = None,
    ai_seed: Optional[str] = None,
    human_source_file_ref: Optional[str] = None,
    human_modification_description: Optional[str] = None,
) -> str:
    """Logs an AI-generated asset into the active workspace's docs asset ledger.

    Maintains a strict legal paper trail required for Steam AI disclosures,
    copyright defense, and hybrid human-AI authorship provenance tracking.
    Writes directly to `./docs/assets_ledger.json` in the current active workspace.

    Args:
        file_name: Path or filename of the final shipped asset in the repository.
        generation_type: Pipeline methodology used ('pure_ai', 'sketch_to_ai', 'ai_to_post_edit', 'sketch_to_ai_to_post_edit').
        base_human_sketch_ref: Reference path to original concept sketch (for img2img pipelines).
        ai_prompt_used: Full prompt text used for AI generation.
        ai_model_used: Optional model identifier (e.g., 'Stable Diffusion XL', 'Midjourney v6').
        ai_seed: Optional generation seed for reproducibility.
        human_source_file_ref: Path to layered project file (e.g., .psd, .kra) proving human edits.
        human_modification_description: Brief summary of human post-edits.

    Returns:
        A confirmation message indicating successful recording in the active workspace ledger.

    Raises:
        ValueError: If generation_type is invalid.
    """
    if generation_type not in VALID_GENERATION_TYPES:
        raise ValueError(
            f"Invalid generation_type '{generation_type}'. Must be one of: {sorted(list(VALID_GENERATION_TYPES))}"
        )

    active_workspace = Path(os.getcwd()).resolve()

    # If running inside a submodule, resolve to the parent containing project.
    # Otherwise, resolve to the top-level git repository.
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-superproject-working-tree"],
            cwd=active_workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        super_project = res.stdout.strip()
        if super_project:
            active_workspace = Path(super_project).resolve()
        else:
            res = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=active_workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            git_root = res.stdout.strip()
            if git_root:
                active_workspace = Path(git_root).resolve()
    except Exception:
        pass
    docs_dir = active_workspace / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = docs_dir / "assets_ledger.json"

    ledger_entries: List[dict] = []
    if ledger_path.exists():
        try:
            with open(ledger_path, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, list):
                    ledger_entries = content
        except (json.JSONDecodeError, OSError):
            ledger_entries = []

    new_entry = {
        "file_name": file_name,
        "generation_type": generation_type,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    if base_human_sketch_ref:
        new_entry["base_human_sketch_ref"] = base_human_sketch_ref

    if ai_prompt_used:
        ai_gen_data = {"prompt": ai_prompt_used}
        if ai_model_used:
            ai_gen_data["model_used"] = ai_model_used
        if ai_seed:
            ai_gen_data["seed"] = ai_seed
        new_entry["ai_generation_data"] = ai_gen_data

    if human_source_file_ref and human_modification_description:
        new_entry["human_post_edit_data"] = {
            "source_file_ref": human_source_file_ref,
            "modification_description": human_modification_description,
        }

    ledger_entries.append(new_entry)
    with open(ledger_path, "w", encoding="utf-8") as f:
        json.dump(ledger_entries, f, indent=2, ensure_ascii=False)

    return (
        f"Successfully logged AI asset '{file_name}' [{generation_type}] to workspace ledger at "
        f"{ledger_path} [UTC: {new_entry['timestamp_utc']}]."
    )


@mcp.tool()
def verify_environment_isolation(workspace_dir: Optional[str] = None) -> str:
    """Verifies that the current workspace and execution environment comply with strict container and network isolation protocols.

    Checks:
    1. Git user email in the active workspace matches approved domains
       (default: '@crenet.games', configurable via ALLOWED_EMAIL_DOMAINS).
    2. Checks that no unauthorized external network bindings or restricted profiles are active in the system process list
       (configurable via RESTRICTED_NETWORK_PROFILES).

    Args:
        workspace_dir: Optional directory path to evaluate. Defaults to current working directory.

    Returns:
        A success message if all airgap compliance checks pass.

    Raises:
        RuntimeError: If a corporate git email or active unauthorized network profile is detected.
    """
    target_dir = Path(workspace_dir).resolve() if workspace_dir else Path(os.getcwd()).resolve()

    # 1. Verify Git Config Email
    try:
        res = subprocess.run(
            ["git", "config", "user.email"],
            cwd=target_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        git_email = res.stdout.strip().lower()
    except Exception as exc:
        raise RuntimeError(f"Environment Isolation Check Error: Failed to execute git config check: {exc}")

    if not git_email:
        raise RuntimeError(
            "Environment Isolation Violation: No Git user.email configured in active workspace. "
            "Please configure user.email with an authorized Crenet Games address."
        )

    is_compliant_email = any(git_email.endswith(domain) for domain in APPROVED_EMAIL_DOMAINS)

    if not is_compliant_email:
        raise RuntimeError(
            f"Environment Isolation Violation: Git email '{git_email}' is non-compliant. "
            f"Commits are blocked. Expected an email ending in one of: {APPROVED_EMAIL_DOMAINS}."
        )

    # 2. Check for active unauthorized network profiles
    try:
        ps_res = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            check=False,
        )
        ps_output = ps_res.stdout.lower()
        for kw in RESTRICTED_NETWORK_KEYWORDS:
            if kw in ps_output:
                raise RuntimeError(
                    f"Environment Isolation Violation: Unauthorized network profile '{kw}' detected. "
                    "All code commits and cloud deployments are strictly blocked."
                )
    except RuntimeError:
        raise
    except Exception:
        # Fallback check via environment variables if ps aux is unavailable
        env_str = str(os.environ).lower()
        for kw in RESTRICTED_NETWORK_KEYWORDS:
            if kw in env_str:
                raise RuntimeError(
                    f"Environment Isolation Violation: Unauthorized network environment indicator '{kw}' detected."
                )

    return (
        f"Environment Isolation Check Passed: Git email '{git_email}' is compliant and no unauthorized "
        "network profiles were detected."
    )


if __name__ == "__main__":
    mcp.run()
