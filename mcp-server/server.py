"""Crenet Games Core Standards MCP Server.

Provides centralized governance, compliance checks, legal paper trail logging,
and deterministic atomic commit chain orchestration for human developers and
AI Agents operating across Crenet Games repositories.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import List, Optional

try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

# Ensure scripts/ directory is importable for Single Source of Truth delegation
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import atomic_commit

# Instantiate the central Model Context Protocol server for Crenet Games
mcp = FastMCP("CrenetGovernance")

# Approved compliant email domains
_ALLOWED_DOMAINS_ENV = os.environ.get("ALLOWED_EMAIL_DOMAINS", "@crenet.games")
APPROVED_EMAIL_DOMAINS = tuple(
    domain.strip().lower() for domain in _ALLOWED_DOMAINS_ENV.split(",") if domain.strip()
)

# Restricted network indicators to flag in process scans
_RESTRICTED_NETWORK_ENV = os.environ.get(
    "RESTRICTED_NETWORK_PROFILES",
    "cisco,anyconnect,globalprotect,zscaler,pulse,forticlient",
)
RESTRICTED_NETWORK_KEYWORDS = tuple(
    kw.strip().lower() for kw in _RESTRICTED_NETWORK_ENV.split(",") if kw.strip()
)

# Allowed generation pipeline methodologies
VALID_GENERATION_TYPES = {
    "pure_ai",
    "sketch_to_ai",
    "ai_to_post_edit",
    "sketch_to_ai_to_post_edit",
}


def _resolve_target_workspace(
    workspace_dir: Optional[str] = None,
    file_hint: Optional[str] = None,
) -> Path:
    """Resolve the active target repository root, avoiding the MCP daemon's cwd trap."""
    if workspace_dir:
        base_dir = Path(workspace_dir).resolve()
    elif file_hint and Path(file_hint).is_absolute():
        hint_path = Path(file_hint).resolve()
        base_dir = hint_path if hint_path.is_dir() else hint_path.parent
    else:
        base_dir = Path(os.getcwd()).resolve()

    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-superproject-working-tree"],
            cwd=base_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        super_project = res.stdout.strip()
        if super_project:
            return Path(super_project).resolve()

        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=base_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        git_root = res.stdout.strip()
        if git_root:
            return Path(git_root).resolve()
    except Exception:
        pass

    return base_dir


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
    workspace_dir: Optional[str] = None,
) -> str:
    """Logs an AI-generated asset into the active workspace's docs asset ledger.

    Maintains a strict legal paper trail required for Steam AI disclosures,
    copyright defense, and hybrid human-AI authorship provenance tracking.
    Writes directly to `./docs/assets_ledger.json` in the target workspace.

    Args:
        file_name: Path or filename of the final shipped asset in the repository.
        generation_type: Pipeline methodology used ('pure_ai', 'sketch_to_ai', 'ai_to_post_edit', 'sketch_to_ai_to_post_edit').
        base_human_sketch_ref: Reference path to original concept sketch (for img2img pipelines).
        ai_prompt_used: Full prompt text used for AI generation.
        ai_model_used: Optional model identifier (e.g., 'Stable Diffusion XL', 'Midjourney v6').
        ai_seed: Optional generation seed for reproducibility.
        human_source_file_ref: Path to layered project file (e.g., .psd, .kra) proving human edits.
        human_modification_description: Brief summary of human post-edits.
        workspace_dir: Optional root directory of the target project repository.

    Returns:
        A confirmation message indicating successful recording in the target workspace ledger.

    Raises:
        ValueError: If generation_type is invalid.
    """
    if generation_type not in VALID_GENERATION_TYPES:
        raise ValueError(
            f"Invalid generation_type '{generation_type}'. Must be one of: {sorted(list(VALID_GENERATION_TYPES))}"
        )

    active_workspace = _resolve_target_workspace(workspace_dir=workspace_dir, file_hint=file_name)
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
        raise RuntimeError(
            f"Environment Isolation Check Error: Failed to execute git config check: {exc}"
        )

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


@mcp.tool()
def atomic_commit_status(workspace_dir: str = ".") -> dict:
    """Inspects dirty/untracked files, hunk counts, static import graph, and topological commit waves.

    Read-only tool that analyzes the target Git repository to help partition uncommitted
    changes into a dependency-ordered atomic commit chain.

    Args:
        workspace_dir: Path to the target Git repository or workspace directory.

    Returns:
        Dictionary containing `repo_root`, `total_dirty_files`, `files`, `topological_waves`,
        and `mutual_dependency_cycles`.
    """
    repo = atomic_commit.get_repo_root(Path(workspace_dir).resolve())
    return atomic_commit.inspect_status(repo)


@mcp.tool()
def atomic_commit_hunks(workspace_dir: str, file: str, cached: bool = False) -> dict:
    """Lists numbered diff hunks for a file to prepare non-interactive partial staging.

    Read-only tool that parses `git diff` into 1-based numbered hunks with line ranges
    and previews so specific hunks can be staged via `atomic_commit_stage`.

    Args:
        workspace_dir: Path to the target Git repository or workspace directory.
        file: Repository-relative path to the file to inspect.
        cached: If True, inspect already-staged hunks in the index instead of unstaged working-tree hunks.

    Returns:
        Dictionary containing `file`, `hunk_count`, and `hunks` metadata.
    """
    repo = atomic_commit.get_repo_root(Path(workspace_dir).resolve())
    fdiff = atomic_commit.parse_single_file_diff(repo, file, cached=cached)
    return {
        "file": fdiff.file_path,
        "hunk_count": len(fdiff.hunks),
        "hunks": [
            {
                "index": h.index,
                "header": h.header.strip(),
                "old_start": h.old_start,
                "old_count": h.old_count,
                "new_start": h.new_start,
                "new_count": h.new_count,
                "section_hint": h.section_hint,
                "preview": h.preview,
            }
            for h in fdiff.hunks
        ],
    }


@mcp.tool()
def atomic_commit_stage(
    workspace_dir: str,
    files: Optional[List[str]] = None,
    hunks: Optional[List[str]] = None,
    reset: bool = False,
) -> dict:
    """Non-interactively stages full files and/or specific numbered hunks into the Git index.

    Args:
        workspace_dir: Path to the target Git repository or workspace directory.
        files: Optional list of full repository-relative file paths to stage via `git add`.
        hunks: Optional list of partial-file hunk specs in the format `'path/to/file:1,3'`.
        reset: If True, unstages all currently staged changes (`git reset`) before staging this slice.

    Returns:
        Dictionary containing `staged_files` currently in the Git index.
    """
    repo = atomic_commit.get_repo_root(Path(workspace_dir).resolve())
    if reset:
        if atomic_commit.has_head_commit(repo):
            atomic_commit.run_git(["reset"], repo)
        else:
            atomic_commit.run_git(["rm", "--cached", "-r", "--quiet", "."], repo, check=False)

    if files:
        atomic_commit.run_git(["add", "--", *files], repo)

    if hunks:
        for spec in hunks:
            file_path, indices = atomic_commit._parse_hunk_spec(spec)
            atomic_commit.stage_file_hunks(repo, file_path, indices)

    staged = atomic_commit.get_staged_files(repo)
    return {"staged_files": sorted(staged.keys())}


@mcp.tool()
def atomic_commit_verify(
    workspace_dir: str = ".",
    commands: Optional[List[str]] = None,
    skip_worktree: bool = False,
) -> dict:
    """Deterministically verifies that the staged slice (`HEAD + index`) is self-contained and passes checks.

    Performs static index-vs-worktree dependency isolation checks (ensuring staged files do not
    import unstaged/untracked files or symbols from unstaged hunks), checks for conflict markers
    and unlogged image assets, and executes `commands` inside an isolated ephemeral `git write-tree`
    worktree without disturbing the developer's working tree.

    Args:
        workspace_dir: Path to the target Git repository or workspace directory.
        commands: Optional list of shell commands (e.g., typecheck, unit tests) to run in the isolated worktree.
        skip_worktree: If True, only run fast static index-vs-worktree checks without creating a temporary worktree.

    Returns:
        Dictionary with `passed` (bool), `staged_files`, `errors`, and `command_results`.
    """
    repo = atomic_commit.get_repo_root(Path(workspace_dir).resolve())
    return atomic_commit.verify_staged_slice(
        repo,
        commands=commands or [],
        skip_worktree=skip_worktree,
    )


if __name__ == "__main__":
    mcp.run()
