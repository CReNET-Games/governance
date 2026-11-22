#!/usr/bin/env python3
"""CrenetGovernance FastMCP server.

Exposes asset provenance tools (`log_asset_provenance`, `move_asset_provenance`,
`audit_asset_provenance`) and atomic commit workflow tools (`atomic_commit_*`).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional

from fastmcp import FastMCP

REPO_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(REPO_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_SCRIPTS_DIR))

import atomic_commit  # noqa: E402
import ledger  # noqa: E402

mcp = FastMCP("CrenetGovernance")


def _resolve_workspace(workspace_dir: str = ".") -> Path:
    resolved = Path(workspace_dir).resolve()
    try:
        return atomic_commit.get_repo_root(resolved)
    except Exception:
        return resolved


@mcp.tool()
def log_asset_provenance(
    asset_path: str,
    steps: List[dict[str, Any]],
    status: str = "active",
    notes: Optional[str] = None,
    previous_paths: Optional[List[str]] = None,
    append_steps: bool = False,
    workspace_dir: str = ".",
) -> dict[str, Any]:
    """Logs or updates a visual/audio asset's step-by-step provenance recipe in docs/assets_ledger.json.

    Supports both published assets and Tier 1 scratch intermediates under `artifacts/`.
    Computes `ai_involvement` (`none` | `assisted` | `generated`) automatically from `steps`
    and computes the current SHA-256 hash when the file exists on disk.

    Args:
        asset_path: Repository-relative path to the asset file.
        steps: List of 1..6 ordered provenance step dicts, each with `actor`
            ('human' | 'ai' | 'procedural'), `tool`, `summary`, and optional
            `prompt` (required iff `actor == 'ai'`), `artifact_ref`, `sha256`,
            `seed`, and `license`.
        status: Asset lifecycle status ('active' or 'deprecated').
        notes: Optional studio context or licensing notes.
        previous_paths: Optional prior paths (for assets copied/graduated manually).
        append_steps: If True, append `steps` to an existing entry's step list.
        workspace_dir: Path to the target repository root.

    Returns:
        The validated v2 ledger entry dictionary persisted to `docs/assets_ledger.json`.
    """
    repo = _resolve_workspace(workspace_dir)
    return ledger.log_asset(
        repo,
        asset_path,
        steps,
        status=status,
        notes=notes,
        previous_paths=previous_paths,
        append_steps=append_steps,
    )


@mcp.tool()
def move_asset_provenance(
    old_path: str,
    new_path: str,
    actor: Optional[str] = None,
    tool: Optional[str] = None,
    summary: Optional[str] = None,
    prompt: Optional[str] = None,
    notes: Optional[str] = None,
    workspace_dir: str = ".",
) -> dict[str, Any]:
    """Graduates or relocates a tracked asset entry while preserving its earlier provenance steps.

    Appends `old_path` to `previous_paths`, optionally appends a relocation/conversion step,
    recomputes `sha256` at `new_path`, and updates `docs/assets_ledger.json`.

    Args:
        old_path: Existing repository-relative path in the ledger (e.g. `artifacts/...`).
        new_path: Destination repository-relative path (e.g. `client/public/...`).
        actor: Optional step actor for the relocation/conversion step.
        tool: Optional tool used for conversion/graduation.
        summary: Optional summary of the graduation/edit step.
        prompt: Optional prompt if `actor == 'ai'`.
        notes: Optional replacement notes string.
        workspace_dir: Path to the target repository root.

    Returns:
        The relocated and revalidated v2 ledger entry dictionary.
    """
    repo = _resolve_workspace(workspace_dir)
    move_step: Optional[dict[str, Any]] = None
    if actor or tool or summary:
        if not (actor and tool and summary):
            raise ValueError("Provide actor, tool, and summary together when adding a move step.")
        move_step = {"actor": actor, "tool": tool, "summary": summary}
        if prompt is not None:
            move_step["prompt"] = prompt

    return ledger.move_asset(repo, old_path, new_path, step=move_step, notes=notes)


@mcp.tool()
def audit_asset_provenance(
    workspace_dir: str = ".",
    paths: Optional[List[str]] = None,
    strict_hashes: bool = False,
    include_report: bool = True,
) -> dict[str, Any]:
    """Audits repository and artifacts/ assets against docs/assets_ledger.json.

    Detects unlogged active assets, orphan ledger entries, stale SHA-256 digests,
    and candidate graduation moves, and optionally returns a Publisher/M&A scorecard.

    Args:
        workspace_dir: Path to the target repository root.
        paths: Optional subset of repository-relative paths to check.
        strict_hashes: If True, mark stale SHA-256 digests as verification failure.
        include_report: If True, attach `report` and `scorecard_markdown` fields.

    Returns:
        Audit dictionary with `enabled`, `ok`, `unlogged`, `orphan_entries`,
        `stale_hashes`, `graduation_candidates`, and optional scorecard data.
    """
    repo = _resolve_workspace(workspace_dir)
    result = ledger.verify_ledger(repo, paths=paths, strict_hashes=strict_hashes)
    if include_report:
        report = ledger.generate_report(repo)
        result["report"] = report
        result["scorecard_markdown"] = ledger.format_report_markdown(report)
    return result


@mcp.tool()
def atomic_commit_status(
    workspace_dir: str = ".",
    count_tests: Optional[bool] = None,
    count_comments: Optional[bool] = None,
    max_commit_lines: Optional[int] = None,
    max_pr_lines: Optional[int] = None,
) -> dict:
    """Inspects dirty/untracked files, hunk counts, line budgets, static import graph, and topological commit waves.

    Read-only tool that analyzes the target Git repository to help partition uncommitted
    changes into a dependency-ordered atomic commit chain sized for focused review.
    Omitted sizing options inherit `.crenet-governance.json` `"sizing"` settings when present.

    Args:
        workspace_dir: Path to the target Git repository or workspace directory.
        count_tests: Override whether test files count in `effective_lines` budgets (default: config or False).
        count_comments: Override whether comment/docstring lines count in `effective_lines` budgets (default: config or False).
        max_commit_lines: Soft target for effective lines per commit (default: config or 200).
        max_pr_lines: Soft target for effective lines per single PR (default: config or 400).

    Returns:
        Dictionary containing `repo_root`, `total_dirty_files`, `files`, `topological_waves`,
        `mutual_dependency_cycles`, `size_summary`, and `coverage_onboarding`.
    """
    repo = atomic_commit.get_repo_root(Path(workspace_dir).resolve())
    return atomic_commit.inspect_status(
        repo,
        count_tests=count_tests,
        count_comments=count_comments,
        max_commit_lines=max_commit_lines,
        max_pr_lines=max_pr_lines,
    )


@mcp.tool()
def atomic_commit_hunks(workspace_dir: str, file: str, cached: bool = False) -> dict:
    """Lists numbered diff hunks (plus symbol chunks for untracked or large hunks) for partial staging.

    Read-only tool that parses `git diff` into 1-based numbered hunks with line ranges,
    categorized line counts (`logic_lines`, `test_lines`, `comment_lines`), and previews
    so specific hunks or `--lines` ranges can be staged via `atomic_commit_stage`.
    For untracked files (`hunk_count == 0`) or hunks exceeding `max_commit_lines`, also
    includes `symbol_chunks` and `suggested_line_slices`.

    Args:
        workspace_dir: Path to the target Git repository or workspace directory.
        file: Repository-relative path to the file to inspect.
        cached: If True, inspect already-staged hunks in the index instead of unstaged working-tree hunks.

    Returns:
        Dictionary containing `file`, `hunk_count`, `hunks`, and optionally `symbol_chunks`
        and `suggested_line_slices`.
    """
    repo = atomic_commit.get_repo_root(Path(workspace_dir).resolve())
    cfg = atomic_commit.load_sizing_config(repo)
    fdiff, hunk_dicts = atomic_commit.get_file_hunks_with_stats(
        repo, file, cached=cached, sizing_config=cfg
    )
    result: dict = {
        "file": fdiff.file_path,
        "hunk_count": len(fdiff.hunks),
        "hunks": hunk_dicts,
    }
    if not cached and (
        not fdiff.hunks
        or any(hd["logic_lines"] > cfg.max_commit_lines for hd in hunk_dicts)
    ):
        chunk_analysis = atomic_commit.analyze_file_chunks(repo, file, sizing_config=cfg)
        result["symbol_chunks"] = chunk_analysis["chunks"]
        result["suggested_line_slices"] = chunk_analysis["suggested_line_slices"]
    return result


@mcp.tool()
def atomic_commit_stage(
    workspace_dir: str,
    files: Optional[List[str]] = None,
    hunks: Optional[List[str]] = None,
    lines: Optional[List[str]] = None,
    reset: bool = False,
) -> dict:
    """Non-interactively stages full files, numbered hunks, and/or 1-based line ranges into the Git index.

    Args:
        workspace_dir: Path to the target Git repository or workspace directory.
        files: Optional list of full repository-relative file paths to stage via `git add`.
        hunks: Optional list of partial-file hunk specs in the format `'path/to/file:1,3'`.
        lines: Optional list of 1-based line-range specs in the format `'path/to/file:1-60,120-180'` (supports untracked files).
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

    if lines:
        for lspec in lines:
            file_path, ranges = atomic_commit.parse_line_spec(lspec)
            atomic_commit.stage_file_lines(repo, file_path, ranges)

    staged = atomic_commit.get_staged_files(repo)
    return {"staged_files": sorted(staged.keys())}


@mcp.tool()
def atomic_commit_verify(
    workspace_dir: str = ".",
    commands: Optional[List[str]] = None,
    skip_worktree: bool = False,
    max_commit_lines: Optional[int] = None,
    max_commit_files: Optional[int] = None,
    count_tests: Optional[bool] = None,
    count_comments: Optional[bool] = None,
    coverage_cmd: Optional[str] = None,
    coverage_report: Optional[str] = None,
    coverage_mode: Optional[str] = None,
) -> dict:
    """Deterministically verifies that the staged slice (`HEAD + index`) is self-contained and passes checks.

    Performs static index-vs-worktree dependency isolation checks (ensuring staged files do not
    import unstaged/untracked files or symbols from unstaged hunks), checks for conflict markers
    and unlogged image assets, computes commit size metrics with non-blocking sizing warnings,
    executes `commands` inside an isolated ephemeral `git write-tree` worktree without
    disturbing the developer's working tree, and evaluates modular coverage ratchets (`vs_head`
    and `vs_upstream`) or test-pairing fallbacks.

    Args:
        workspace_dir: Path to the target Git repository or workspace directory.
        commands: Optional list of shell commands (e.g., typecheck, unit tests) to run in the isolated worktree.
        skip_worktree: If True, only run fast static index-vs-worktree checks without creating a temporary worktree.
        max_commit_lines: Soft commit size warning threshold in effective lines (default: config or 200).
        max_commit_files: Soft commit scope warning threshold in production files (default: config or 10).
        count_tests: Override whether test files count in `effective_lines` size check (default: config or False).
        count_comments: Override whether comment/docstring lines count in `effective_lines` size check (default: config or False).
        coverage_cmd: Optional override shell command to generate a coverage report inside the worktree.
        coverage_report: Optional worktree-relative coverage report path (Istanbul JSON, coverage.json, lcov.info, or coverage.xml).
        coverage_mode: Optional override enforcement mode ('warn', 'error', or 'off').

    Returns:
        Dictionary with `passed` (bool), `staged_files`, `errors`, `warnings`, `size_metrics`,
        `coverage`, and `command_results`.
    """
    repo = atomic_commit.get_repo_root(Path(workspace_dir).resolve())
    return atomic_commit.verify_staged_slice(
        repo,
        commands=commands or [],
        skip_worktree=skip_worktree,
        max_commit_lines=max_commit_lines,
        max_commit_files=max_commit_files,
        count_tests=count_tests,
        count_comments=count_comments,
        coverage_cmd=coverage_cmd,
        coverage_reports=[coverage_report] if coverage_report else None,
        coverage_mode=coverage_mode,
    )


if __name__ == "__main__":
    mcp.run()
