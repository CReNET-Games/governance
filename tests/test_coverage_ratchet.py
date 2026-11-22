"""Parameterized unit tests for scripts/coverage_ratchet.py."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

import coverage_ratchet  # noqa: E402


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "cov_unit_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@crenet.games"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Dev"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.parametrize(
    ("setup_kind", "expected"),
    [
        pytest.param("missing", None, id="missing_file"),
        pytest.param("binary_null", None, id="binary_null_bytes"),
        pytest.param("valid_utf8", "hello\n", id="valid_utf8"),
    ],
)
def test_read_utf8_safe(git_repo: Path, setup_kind: str, expected: str | None):
    """Verifies _read_utf8_safe returns text for UTF-8 files and None for missing or binary files."""
    target = git_repo / "sample.dat"
    if setup_kind == "binary_null":
        target.write_bytes(b"\x00\xff")
    elif setup_kind == "valid_utf8":
        target.write_bytes(b"hello\n")
    assert coverage_ratchet._read_utf8_safe(target) == expected


@pytest.mark.parametrize(
    (
        "worktree_json",
        "staged_json",
        "cli_kwargs",
        "expected_state",
        "expected_mode",
        "expected_cmd",
        "expected_reports",
    ),
    [
        pytest.param(None, None, {}, "unconfigured", "warn", None, [], id="unconfigured_default"),
        pytest.param(None, None, {"cli_mode": "off"}, "opted_out", "warn", None, [], id="cli_mode_off"),
        pytest.param("{invalid json", None, {}, "unconfigured", "warn", None, [], id="corrupt_json"),
        pytest.param(
            '{"coverage": {"command": "pytest --cov", "report_path": "cov.json", "mode": "error"}}',
            None,
            {},
            "configured",
            "error",
            "pytest --cov",
            ["cov.json"],
            id="worktree_configured",
        ),
        pytest.param(
            '{"coverage": {"enabled": false}}',
            '{"coverage": {"command": "pytest --cov", "report_paths": ["idx.json"], "mode": "error"}}',
            {"cli_cmd": "pytest -q", "cli_reports": ["custom.xml"], "cli_mode": "warn"},
            "configured",
            "warn",
            "pytest -q",
            ["custom.xml"],
            id="staged_index_and_cli_override",
        ),
    ],
)
def test_load_coverage_config(
    git_repo: Path,
    worktree_json: str | None,
    staged_json: str | None,
    cli_kwargs: dict,
    expected_state: str,
    expected_mode: str,
    expected_cmd: str | None,
    expected_reports: list[str],
):
    """Verifies load_coverage_config precedence across defaults, JSON, staged index, and CLI flags."""
    cfg_file = git_repo / ".crenet-governance.json"
    staged_map = None
    if staged_json is not None:
        cfg_file.write_text(staged_json, encoding="utf-8")
        subprocess.run(["git", "add", ".crenet-governance.json"], cwd=git_repo, check=True)
        staged_map = {".crenet-governance.json": "A"}
    if worktree_json is not None:
        cfg_file.write_text(worktree_json, encoding="utf-8")

    cfg = coverage_ratchet.load_coverage_config(git_repo, staged=staged_map, **cli_kwargs)
    assert cfg.state == expected_state
    assert cfg.mode == expected_mode
    assert cfg.command == expected_cmd
    assert cfg.report_paths == expected_reports


