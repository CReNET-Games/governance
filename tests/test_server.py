"""Tests for mcp-server/server.py FastMCP governance tools."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../mcp-server")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

import server  # noqa: E402
from server import (  # noqa: E402
    atomic_commit_hunks,
    atomic_commit_stage,
    atomic_commit_status,
    atomic_commit_verify,
    audit_asset_provenance,
    log_asset_provenance,
    mcp,
    move_asset_provenance,
)


def call_fn(tool_or_fn, *args, **kwargs):
    fn = getattr(tool_or_fn, "fn", tool_or_fn)
    return fn(*args, **kwargs)


def test_mcp_server_exposes_expected_tools_only():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert {
        "log_asset_provenance",
        "move_asset_provenance",
        "audit_asset_provenance",
        "atomic_commit_status",
        "atomic_commit_hunks",
        "atomic_commit_stage",
        "atomic_commit_verify",
    } <= names
    assert "verify_environment_isolation" not in names
    assert "log_ai_asset" not in names
    assert not hasattr(server, "verify_environment_isolation")
    assert not hasattr(server, "log_ai_asset")


def test_log_and_move_and_audit_asset_provenance_lifecycle(tmp_path: Path):
    draft = tmp_path / "artifacts/storefront/keyart_raw.png"
    draft.parent.mkdir(parents=True)
    draft.write_bytes(b"raw-keyart-bytes")

    logged = call_fn(
        log_asset_provenance,
        asset_path="artifacts/storefront/keyart_raw.png",
        steps=[
            {
                "actor": "ai",
                "tool": "gemini-imagen",
                "prompt": "Ligne claire void black corridor with cyan extraction silo",
                "summary": "Initial AI composition.",
            }
        ],
        workspace_dir=str(tmp_path),
    )
    assert logged["ai_involvement"] == "generated"
    assert len(logged["sha256"]) == 64

    published = tmp_path / "client/public/og-banner-1200x630.png"
    published.parent.mkdir(parents=True)
    draft.rename(published)

    moved = call_fn(
        move_asset_provenance,
        old_path="artifacts/storefront/keyart_raw.png",
        new_path="client/public/og-banner-1200x630.png",
        actor="human",
        tool="GIMP",
        summary="Cropped to 1200x630 and added Rajdhani typography.",
        workspace_dir=str(tmp_path),
    )
    assert moved["asset_path"] == "client/public/og-banner-1200x630.png"
    assert moved["previous_paths"] == ["artifacts/storefront/keyart_raw.png"]
    assert moved["ai_involvement"] == "assisted"

    audit = call_fn(audit_asset_provenance, workspace_dir=str(tmp_path))
    assert audit["ok"] is True
    assert audit["report"]["counts"]["ai_involvement"]["assisted"] == 1
    assert "Asset Provenance Scorecard" in audit["scorecard_markdown"]


def test_log_asset_provenance_rejects_invalid_steps(tmp_path: Path):
    with pytest.raises(ValueError):
        call_fn(
            log_asset_provenance,
            asset_path="bad.png",
            steps=[{"actor": "ai", "tool": "Imagen", "summary": "Missing prompt"}],
            workspace_dir=str(tmp_path),
        )


def test_atomic_commit_mcp_tools_workflow(tmp_path: Path):
    repo = tmp_path / "mcp_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@crenet.games"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    (repo / "shared.ts").write_text("export const X = 1;\n", encoding="utf-8")
    (repo / "app.ts").write_text("import { X } from './shared';\nconsole.log(X);\n", encoding="utf-8")

    status = call_fn(atomic_commit_status, workspace_dir=str(repo))
    assert status["topological_waves"] == [["shared.ts"], ["app.ts"]]

    call_fn(atomic_commit_stage, workspace_dir=str(repo), files=["shared.ts"], reset=True)
    verify = call_fn(atomic_commit_verify, workspace_dir=str(repo), skip_worktree=True)
    assert verify["passed"] is True
    hunks = call_fn(atomic_commit_hunks, workspace_dir=str(repo), file="shared.ts", cached=True)
    assert "hunk_count" in hunks


@pytest.fixture
def mcp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "mcp_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@crenet.games"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.parametrize(
    ("count_tests", "count_comments", "expected_eff_lines"),
    [
        pytest.param(False, False, 11, id="default_logic_only"),
        pytest.param(True, True, 28, id="include_tests_and_comments"),
    ],
)
def test_atomic_commit_mcp_sizing_options_and_warnings(
    mcp_git_repo: Path,
    count_tests: bool,
    count_comments: bool,
    expected_eff_lines: int,
):
    """Verifies MCP tools expose size_summary, per-hunk line stats, size_metrics, and non-blocking warnings."""
    (mcp_git_repo / "feature.py").write_text("x = 1\n# old\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.py"], cwd=mcp_git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add feature"], cwd=mcp_git_repo, check=True, capture_output=True)

    (mcp_git_repo / "feature.py").write_text(
        "# new comment\n" + "".join(f"v_{i} = {i}\n" for i in range(10)),
        encoding="utf-8",
    )
    (mcp_git_repo / "tests").mkdir()
    (mcp_git_repo / "tests/test_feature.py").write_text(
        "".join(f"def test_f_{i}(): assert True\n" for i in range(15)),
        encoding="utf-8",
    )

    status = call_fn(
        atomic_commit_status,
        workspace_dir=str(mcp_git_repo),
        count_tests=count_tests,
        count_comments=count_comments,
        max_commit_lines=8,
        max_pr_lines=10,
    )
    ss = status["size_summary"]
    assert ss["logic_lines"] == 11
    assert ss["test_lines"] == 15
    assert ss["effective_lines"] == expected_eff_lines
    assert any("PR size warning" in w for w in ss["warnings"])

    hunks = call_fn(atomic_commit_hunks, workspace_dir=str(mcp_git_repo), file="feature.py")
    assert hunks["hunk_count"] == 1
    assert hunks["hunks"][0]["logic_lines"] == 11
    assert hunks["hunks"][0]["comment_lines"] == 2

    call_fn(
        atomic_commit_stage,
        workspace_dir=str(mcp_git_repo),
        files=["feature.py", "tests/test_feature.py"],
        reset=True,
    )
    verify = call_fn(
        atomic_commit_verify,
        workspace_dir=str(mcp_git_repo),
        skip_worktree=True,
        count_tests=count_tests,
        count_comments=count_comments,
        max_commit_lines=8,
    )
    assert verify["passed"] is True
    assert verify["size_metrics"]["effective_lines"] == expected_eff_lines
    assert any("Commit size warning" in w for w in verify["warnings"])


@pytest.mark.parametrize(
    ("coverage_mode", "expected_status", "expect_warning"),
    [
        pytest.param(None, "skipped", True, id="default_unconfigured_warns"),
        pytest.param("off", "opted_out", False, id="mode_off_suppresses_warning"),
    ],
)
def test_atomic_commit_mcp_coverage_onboarding_and_verify_params(
    mcp_git_repo: Path,
    coverage_mode: str | None,
    expected_status: str,
    expect_warning: bool,
):
    """Verifies MCP status surfaces coverage_onboarding and verify forwards coverage_mode/report overrides."""
    (mcp_git_repo / "package.json").write_text('{"devDependencies": {"vitest": "^2.0.0"}}\n', encoding="utf-8")
    subprocess.run(["git", "add", "package.json"], cwd=mcp_git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add pkg"], cwd=mcp_git_repo, check=True, capture_output=True)

    (mcp_git_repo / "src.ts").write_text("export const v = 42;\n", encoding="utf-8")
    status = call_fn(atomic_commit_status, workspace_dir=str(mcp_git_repo))
    assert status["coverage_onboarding"] is not None
    assert status["coverage_onboarding"]["detected_stack"] == "vitest"

    call_fn(atomic_commit_stage, workspace_dir=str(mcp_git_repo), files=["src.ts"], reset=True)
    verify = call_fn(
        atomic_commit_verify,
        workspace_dir=str(mcp_git_repo),
        skip_worktree=True,
        coverage_mode=coverage_mode,
    )
    assert verify["coverage"]["status"] == expected_status
    assert bool(any("Test pairing warning" in w for w in verify["warnings"])) is expect_warning


@pytest.mark.parametrize(
    ("line_spec", "expected_passed", "expected_err_substr"),
    [
        pytest.param("big.py:3-5", False, "symbol 'base_a'", id="missing_intrafile_dep_fails"),
        pytest.param("big.py:1-2", True, None, id="bottom_up_slice_passes"),
    ],
)
def test_atomic_commit_mcp_untracked_chunks_and_lines_staging(
    mcp_git_repo: Path,
    line_spec: str,
    expected_passed: bool,
    expected_err_substr: str | None,
):
    """Verifies MCP hunks returns symbol_chunks for untracked files and stage supports lines=[...]."""
    (mcp_git_repo / ".crenet-governance.json").write_text('{"coverage": false}\n', encoding="utf-8")
    subprocess.run(["git", "add", ".crenet-governance.json"], cwd=mcp_git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "opt out"], cwd=mcp_git_repo, check=True, capture_output=True)

    (mcp_git_repo / "big.py").write_text(
        "def base_a() -> int:\n    return 1\n\n"
        "def caller_b() -> int:\n    return base_a() + 2\n",
        encoding="utf-8",
    )

    hunks_info = call_fn(atomic_commit_hunks, workspace_dir=str(mcp_git_repo), file="big.py")
    assert hunks_info["hunk_count"] == 0
    assert len(hunks_info["symbol_chunks"]) == 2
    assert len(hunks_info["suggested_line_slices"]) >= 1

    call_fn(
        atomic_commit_stage,
        workspace_dir=str(mcp_git_repo),
        lines=[line_spec],
        reset=True,
    )
    verify = call_fn(atomic_commit_verify, workspace_dir=str(mcp_git_repo), skip_worktree=True)
    assert verify["passed"] is expected_passed
    if expected_err_substr is None:
        assert verify["errors"] == []
    else:
        assert any(expected_err_substr in e for e in verify["errors"])
