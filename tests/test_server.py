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
