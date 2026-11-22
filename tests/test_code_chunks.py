"""Unit and integration tests for scripts/code_chunks.py."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

import code_chunks  # noqa: E402


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "chunks_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@crenet.games"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Dev"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("src/mod.py:1-10,25-30", ("src/mod.py", [(1, 10), (25, 30)])),
        ("src/mod.py:7", ("src/mod.py", [(7, 7)])),
    ],
)
def test_parse_line_spec_valid_and_invalid(spec: str, expected):
    """Verifies line-spec parser handles ranges, single lines, and rejects invalid syntax."""
    assert code_chunks.parse_line_spec(spec) == expected

    with pytest.raises(ValueError):
        code_chunks.parse_line_spec("missing_colon.py")
    with pytest.raises(ValueError):
        code_chunks.parse_line_spec("a.py:10-5")
    with pytest.raises(ValueError):
        code_chunks.parse_line_spec("a.py:0-3")


def test_progressive_stage_file_lines_on_untracked_file(git_repo: Path):
    """Verifies --lines progressively commits an untracked file in 3 slices without touching working tree."""
    target = git_repo / "module.py"
    wt_text = (
        "from __future__ import annotations\n"
        "\n"
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "def middle(x: int) -> int:\n"
        "    return helper(x) * 2\n"
        "\n"
        "def tail(x: int) -> int:\n"
        "    return middle(x) - 1\n"
    )
    target.write_text(wt_text, encoding="utf-8")

    # Slice 1: header + helper (lines 1-4)
    _, r1 = code_chunks.parse_line_spec("module.py:1-4")
    code_chunks.stage_file_lines(git_repo, "module.py", r1)
    blob1 = subprocess.check_output(
        ["git", "show", ":module.py"], cwd=git_repo, text=True
    )
    assert "def helper" in blob1
    assert "def middle" not in blob1
    assert target.read_text(encoding="utf-8") == wt_text
    subprocess.run(["git", "commit", "-m", "feat: add helper"], cwd=git_repo, check=True, capture_output=True)

    # Slice 2: middle (lines 5-7)
    _, r2 = code_chunks.parse_line_spec("module.py:5-7")
    code_chunks.stage_file_lines(git_repo, "module.py", r2)
    blob2 = subprocess.check_output(
        ["git", "show", ":module.py"], cwd=git_repo, text=True
    )
    assert "def helper" in blob2 and "def middle" in blob2 and "def tail" not in blob2
    subprocess.run(["git", "commit", "-m", "feat: add middle"], cwd=git_repo, check=True, capture_output=True)

    # Slice 3: tail (lines 8-10) -> index matches working tree 100%
    _, r3 = code_chunks.parse_line_spec("module.py:8-10")
    code_chunks.stage_file_lines(git_repo, "module.py", r3)
    blob3 = subprocess.check_output(
        ["git", "show", ":module.py"], cwd=git_repo, text=True
    )
    assert blob3 == wt_text
    subprocess.run(["git", "commit", "-m", "feat: add tail"], cwd=git_repo, check=True, capture_output=True)
    assert target.read_text(encoding="utf-8") == wt_text


def test_stage_file_lines_splits_single_tracked_hunk_and_handles_deletes(git_repo: Path):
    """Verifies --lines splits a single contiguous hunk and selectively stages deletions in tracked files."""
    p = git_repo / "app.py"
    p.write_text("a = 1\nb = 2\nlegacy = 9\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init app"], cwd=git_repo, check=True, capture_output=True)

    p.write_text("a = 100\nb = 200\n", encoding="utf-8")
    # Stage only line 1 change (keep old b = 2 and legacy = 9 unstaged)
    code_chunks.stage_file_lines(git_repo, "app.py", [(1, 1)])
    blob = subprocess.check_output(["git", "show", ":app.py"], cwd=git_repo, text=True)
    assert blob == "a = 100\nb = 2\nlegacy = 9\n"

    # Stage remaining line 2 (which also triggers adjacent delete of legacy = 9)
    code_chunks.stage_file_lines(git_repo, "app.py", [(2, 2)])
    blob_full = subprocess.check_output(["git", "show", ":app.py"], cwd=git_repo, text=True)
    assert blob_full == "a = 100\nb = 200\n"
