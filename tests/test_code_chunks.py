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


@pytest.mark.parametrize(
    ("lang", "source", "expected_symbols"),
    [
        pytest.param(
            "py",
            "import os\nCONST = 1\nclass Box:\n    pass\ndef run():\n    return Box()\n",
            [["os"], ["CONST"], ["Box"], ["run"]],
            id="python_top_level",
        ),
        pytest.param("py", "def broken(:\n", [[]], id="python_syntax_fallback"),
        pytest.param(
            "ts",
            "import { Base } from './base';\n\nexport interface Item {\n  id: string;\n}\n\nexport function make(id: string): Item {\n  return { id };\n}\n",
            [[], ["Item"], ["make"]],
            id="typescript_decls",
        ),
        pytest.param("ts", "", [], id="typescript_empty"),
    ],
)
def test_extract_py_and_ts_chunks(
    lang: str, source: str, expected_symbols: list[list[str]]
):
    """Verifies _extract_py_chunks and _extract_ts_chunks top-level symbol extraction."""
    lines = source.splitlines()
    chunks = (
        code_chunks._extract_py_chunks(source, len(lines))
        if lang == "py"
        else code_chunks._extract_ts_chunks(lines)
    )
    assert [c["symbols"] for c in chunks] == expected_symbols


@pytest.mark.parametrize(
    ("chunks", "file_role", "target_max", "expected"),
    [
        pytest.param(
            [
                {
                    "index": 1,
                    "kind": "header",
                    "start_line": 1,
                    "end_line": 3,
                    "symbols": [],
                    "status": "added",
                    "depends_on_symbols": [],
                    "logic_lines": 3,
                    "test_lines": 0,
                },
                {
                    "index": 2,
                    "kind": "function",
                    "start_line": 4,
                    "end_line": 8,
                    "symbols": ["caller"],
                    "status": "added",
                    "depends_on_symbols": ["callee"],
                    "logic_lines": 5,
                    "test_lines": 0,
                },
                {
                    "index": 3,
                    "kind": "function",
                    "start_line": 9,
                    "end_line": 10,
                    "symbols": ["callee"],
                    "status": "added",
                    "depends_on_symbols": [],
                    "logic_lines": 2,
                    "test_lines": 0,
                },
            ],
            "production",
            6,
            [
                {"slice_index": 1, "chunk_indices": [1, 3], "logic_lines": 5, "line_spec": "m.py:1-3,9-10"},
                {"slice_index": 2, "chunk_indices": [2], "logic_lines": 5, "line_spec": "m.py:4-8"},
            ],
            id="header_and_reverse_dep",
        ),
        pytest.param(
            [
                {
                    "index": 1,
                    "kind": "function",
                    "start_line": 1,
                    "end_line": 2,
                    "symbols": ["ping"],
                    "status": "added",
                    "depends_on_symbols": ["pong"],
                    "logic_lines": 2,
                    "test_lines": 0,
                },
                {
                    "index": 2,
                    "kind": "function",
                    "start_line": 3,
                    "end_line": 4,
                    "symbols": ["pong"],
                    "status": "added",
                    "depends_on_symbols": ["ping"],
                    "logic_lines": 2,
                    "test_lines": 0,
                },
            ],
            "production",
            10,
            [{"slice_index": 1, "chunk_indices": [1, 2], "logic_lines": 4, "line_spec": "m.py:1-4"}],
            id="mutual_cycle_collapsed",
        ),
        pytest.param(
            [
                {
                    "index": 1,
                    "kind": "function",
                    "start_line": 1,
                    "end_line": 2,
                    "symbols": ["ok"],
                    "status": "unchanged",
                    "depends_on_symbols": [],
                    "logic_lines": 2,
                    "test_lines": 0,
                }
            ],
            "production",
            10,
            [],
            id="unchanged_only",
        ),
    ],
)
def test_compute_suggested_line_slices_topological_and_cycles(
    chunks: list[dict], file_role: str, target_max: int, expected: list[dict]
):
    """Verifies topological unit packing, header placement, cycle collapsing, and no-dirty empty output."""
    assert (
        code_chunks._compute_suggested_line_slices(
            "m.py", chunks, file_role=file_role, target_max=target_max
        )
        == expected
    )


def test_analyze_file_chunks_python_closure_and_typescript_slices(git_repo: Path):
    """Verifies chunking puts bottom-of-file helper dependencies before callers and handles TS files."""
    py_file = git_repo / "pipeline.py"
    py_file.write_text(
        '"""Pipeline module."""\n'
        "import os\n\n"
        "def stage_one(x: int) -> int:\n"
        "    a = x + 1\n"
        "    b = a + 2\n"
        "    return _bottom_helper(b)\n\n"
        "def independent(y: int) -> int:\n"
        "    p = y * 2\n"
        "    q = p + 3\n"
        "    return q\n\n"
        "def _bottom_helper(v: int) -> int:\n"
        "    return v * 10\n",
        encoding="utf-8",
    )

    py_analysis = code_chunks.analyze_file_chunks(git_repo, "pipeline.py", max_chunk_lines=7)
    slices = py_analysis["suggested_line_slices"]
    assert len(slices) >= 2
    # Slice 1 must include _bottom_helper (chunk 4, lines 13-15) before stage_one (chunk 2)
    helper_chunk = next(c for c in py_analysis["chunks"] if c["symbols"] == ["_bottom_helper"])
    stage_one_chunk = next(c for c in py_analysis["chunks"] if c["symbols"] == ["stage_one"])
    assert helper_chunk["index"] in slices[0]["chunk_indices"]
    assert stage_one_chunk["index"] not in slices[0]["chunk_indices"]
    _, parsed_ranges = code_chunks.parse_line_spec(slices[0]["line_spec"])
    covered = {ln for s, e in parsed_ranges for ln in range(s, e + 1)}
    assert set(range(helper_chunk["start_line"], helper_chunk["end_line"] + 1)) <= covered

    ts_file = git_repo / "view.ts"
    ts_file.write_text(
        "import { Base } from './base';\n\n"
        "export interface Item {\n  id: string;\n}\n\n"
        "export function makeItem(id: string): Item {\n"
        "  return { id };\n"
        "}\n",
        encoding="utf-8",
    )
    ts_analysis = code_chunks.analyze_file_chunks(git_repo, "view.ts", max_chunk_lines=5)
    assert ts_analysis["chunk_count"] == 3
    assert ts_analysis["chunks"][1]["symbols"] == ["Item"]
    assert "Item" in ts_analysis["chunks"][2]["depends_on_symbols"]



def test_chunk_edge_cases_modified_test_file_and_line_errors(git_repo: Path):
    """Verifies line-range validation, modified HEAD chunk statuses, and test_lines chunk packing."""
    test_file = git_repo / "tests" / "test_suite.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "import pytest\n\n"
        "def test_existing():\n"
        "    assert 1 == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "add test"], cwd=git_repo, check=True, capture_output=True)

    test_file.write_text(
        "import pytest\n\n"
        "def test_existing():\n"
        "    assert 2 == 2\n\n"
        "def test_new_a():\n"
        "    a = 1\n"
        "    assert a == 1\n\n"
        "def test_new_b():\n"
        "    b = 2\n"
        "    assert b == 2\n",
        encoding="utf-8",
    )
    analysis = code_chunks.analyze_file_chunks(git_repo, "tests/test_suite.py", max_chunk_lines=4)
    statuses = [c["status"] for c in analysis["chunks"]]
    assert statuses == ["unchanged", "modified", "added", "added"]
    assert len(analysis["suggested_line_slices"]) >= 2

    with pytest.raises(FileNotFoundError):
        code_chunks.stage_file_lines(git_repo, "missing.py", [(1, 1)])
    with pytest.raises(ValueError):
        code_chunks.stage_file_lines(git_repo, "tests/test_suite.py", [(1, 999)])
