"""Data-driven unit and integration tests for scripts/atomic_commit.py."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

import atomic_commit


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create and initialize a temporary Git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "dev@crenet.games"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Dev"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.mark.parametrize(
    ("files_map", "expected_waves", "expected_cycles"),
    [
        pytest.param(
            {
                "shared/types.ts": "export interface Ship { id: string; }\n",
                "server/service.ts": (
                    "import type { Ship } from '../shared/types.js';\n"
                    "export const createShip = (): Ship => ({ id: '1' });\n"
                ),
                "client/ui.ts": (
                    "import { createShip } from '../server/service';\n"
                    "import type { Ship } from '../shared/types';\n"
                    "export const render = (): Ship => createShip();\n"
                ),
            },
            [
                ["shared/types.ts"],
                ["server/service.ts"],
                ["client/ui.ts"],
            ],
            [],
            id="ts_multi_package_esm_chain",
        ),
        pytest.param(
            {
                "core/models.py": "class Player:\n    pass\n",
                "core/engine.py": "from .models import (\n    Player,  # noqa: E402\n)\ndef tick():\n    return Player()\n",
                "app.py": "from core.engine import tick\nfrom core.models import *\n",
            },
            [
                ["core/models.py"],
                ["core/engine.py"],
                ["app.py"],
            ],
            [],
            id="python_relative_and_package_chain",
        ),
        pytest.param(
            {
                "a.py": "import b\nx = 1\n",
                "b.py": "import a\ny = 2\n",
            },
            [["a.py", "b.py"]],
            [["a.py", "b.py"]],
            id="mutual_dependency_cycle",
        ),
    ],
)
def test_status_topological_waves_and_cycles(
    temp_git_repo: Path,
    files_map: dict[str, str],
    expected_waves: list[list[str]],
    expected_cycles: list[list[str]],
):
    """Verifies status resolves TS/JS/Python imports and computes topological waves and cycles."""
    for rel_path, content in files_map.items():
        full_path = temp_git_repo / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    report = atomic_commit.inspect_status(temp_git_repo)
    assert report["total_dirty_files"] == len(files_map)
    assert report["topological_waves"] == expected_waves
    assert report["mutual_dependency_cycles"] == expected_cycles


@pytest.mark.parametrize(
    ("hunk_spec", "expected_staged_Fragments", "expected_unstaged_fragments"),
    [
        pytest.param(
            "module.py:1,3",
            ["line_2 = 200", "line_28 = 2800", "line_15 = 15\n"],
            ["line_15 = 1500"],
            id="non_adjacent_hunks_1_and_3",
        ),
        pytest.param(
            "module.py:2",
            ["line_15 = 1500", "line_2 = 2\n", "line_28 = 28\n"],
            ["line_2 = 200", "line_28 = 2800"],
            id="middle_hunk_only",
        ),
        pytest.param(
            "module.py:1,2,3",
            ["line_2 = 200", "line_15 = 1500", "line_28 = 2800"],
            [],
            id="all_hunks",
        ),
    ],
)
def test_hunks_and_partial_staging(
    temp_git_repo: Path,
    hunk_spec: str,
    expected_staged_Fragments: list[str],
    expected_unstaged_fragments: list[str],
):
    """Verifies non-interactive partial hunk staging across different hunk combinations."""
    lines = [f"line_{i} = {i}\n" for i in range(1, 31)]
    target = temp_git_repo / "module.py"
    target.write_text("".join(lines), encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=temp_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: add module.py"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    lines[1] = "line_2 = 200\nline_2_extra = 201\n"
    lines[14] = "line_15 = 1500\nline_15_extra1 = 1\nline_15_extra2 = 2\n"
    lines[27] = "line_28 = 2800\n"
    modified_worktree_text = "".join(lines)
    target.write_text(modified_worktree_text, encoding="utf-8")

    fdiff = atomic_commit.parse_single_file_diff(temp_git_repo, "module.py")
    assert len(fdiff.hunks) == 3

    rc = atomic_commit.main(
        ["--repo", str(temp_git_repo), "stage", "--reset", "--hunks", hunk_spec]
    )
    assert rc == 0

    staged_blob = atomic_commit.read_index_blob(temp_git_repo, "module.py")
    assert staged_blob is not None
    for frag in expected_staged_Fragments:
        assert frag in staged_blob
    for frag in expected_unstaged_fragments:
        assert frag not in staged_blob

    # Working tree remains untouched
    assert target.read_text(encoding="utf-8") == modified_worktree_text


@pytest.mark.parametrize(
    ("repo_files", "staged_files", "expected_passed", "expected_error_substrings"),
    [
        pytest.param(
            {
                "shared.ts": "export const VERSION = 1;\n",
                "app.ts": "import { VERSION } from './shared';\nconsole.log(VERSION);\n",
            },
            ["app.ts"],
            False,
            ["imports 'shared.ts'", "unstaged/untracked"],
            id="fails_when_imported_ts_file_is_untracked",
        ),
        pytest.param(
            {
                "shared.ts": "export const VERSION = 1;\n",
                "app.ts": "import { VERSION } from './shared';\nconsole.log(VERSION);\n",
            },
            ["shared.ts", "app.ts"],
            True,
            [],
            id="passes_when_imported_ts_file_is_staged",
        ),
        pytest.param(
            {
                "conflict.ts": "<<<<<<< HEAD\nconst a = 1;\n=======\nconst a = 2;\n>>>>>>> br\n",
            },
            ["conflict.ts"],
            False,
            ["merge conflict markers"],
            id="fails_on_merge_conflict_markers",
        ),
        pytest.param(
            {
                "sprite.png": b"\x89PNG\r\n\x1a\n",
            },
            ["sprite.png"],
            False,
            ["sprite.png", "assets_ledger.json"],
            id="fails_when_staged_image_not_in_ledger",
        ),
        pytest.param(
            {
                "sprite.png": b"\x89PNG\r\n\x1a\n",
                "docs/assets_ledger.json": (
                    '{\n'
                    '  "version": 2,\n'
                    '  "assets": {\n'
                    '    "sprite.png": {\n'
                    '      "asset_path": "sprite.png",\n'
                    '      "status": "active",\n'
                    '      "ai_involvement": "none",\n'
                    '      "logged_at": "2026-11-15T00:00:00Z",\n'
                    '      "steps": [{"actor": "human", "tool": "Aseprite", "summary": "Pixel sprite."}]\n'
                    '    }\n'
                    '  }\n'
                    '}\n'
                ),
            },
            ["sprite.png", "docs/assets_ledger.json"],
            True,
            [],
            id="passes_when_staged_image_is_logged_in_v2_ledger",
        ),
        pytest.param(
            {
                "sprite.png": b"\x89PNG\r\n\x1a\n",
                ".crenet-governance.json": '{"asset_provenance": false}\n',
            },
            ["sprite.png", ".crenet-governance.json"],
            True,
            [],
            id="passes_when_asset_provenance_opted_out",
        ),
    ],
)
def test_verify_static_and_hygiene_checks(
    temp_git_repo: Path,
    repo_files: dict[str, str | bytes],
    staged_files: list[str],
    expected_passed: bool,
    expected_error_substrings: list[str],
):
    """Verifies static dependency isolation, conflict markers, and asset ledger checks."""
    for rel_path, content in repo_files.items():
        full_path = temp_git_repo / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            full_path.write_bytes(content)
        else:
            full_path.write_text(content, encoding="utf-8")

    atomic_commit.main(
        ["--repo", str(temp_git_repo), "stage", "--reset", "--files", *staged_files]
    )

    report = atomic_commit.verify_staged_slice(temp_git_repo)
    assert report["passed"] is expected_passed
    joined_errors = "\n".join(report["errors"])
    for substr in expected_error_substrings:
        assert substr in joined_errors


def test_verify_catches_symbol_from_unstaged_hunk_in_partially_staged_file(temp_git_repo: Path):
    """Verifies static check catches importing a symbol that only exists in an unstaged hunk of a partially staged file."""
    math_file = temp_git_repo / "math.ts"
    base_lines = [
        "export const existing = 1;\n",
        *[f"// filler {i}\n" for i in range(1, 20)],
        "export const tail = 2;\n",
    ]
    math_file.write_text("".join(base_lines), encoding="utf-8")
    subprocess.run(["git", "add", "math.ts"], cwd=temp_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: add math.ts"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    updated_lines = list(base_lines)
    updated_lines[0] = "export const existing = 1;\nexport const add = (a: number, b: number) => a + b;\n"
    updated_lines[-1] = "export const tail = 2;\nexport const multiply = (a: number, b: number) => a * b;\n"
    math_file.write_text("".join(updated_lines), encoding="utf-8")

    consumer = temp_git_repo / "calc.ts"
    consumer.write_text(
        "import { add, multiply } from './math';\nexport const run = () => add(1, multiply(2, 3));\n",
        encoding="utf-8",
    )

    # Stage only Hunk 1 of math.ts (`add`) plus calc.ts (which imports both `add` and `multiply`)
    atomic_commit.main(
        [
            "--repo",
            str(temp_git_repo),
            "stage",
            "--reset",
            "--files",
            "calc.ts",
            "--hunks",
            "math.ts:1",
        ]
    )

    report_fail = atomic_commit.verify_staged_slice(temp_git_repo)
    assert report_fail["passed"] is False
    assert any(
        "symbol 'multiply'" in err and "unstaged working-tree changes" in err
        for err in report_fail["errors"]
    )

    # Stage remaining hunk of math.ts and verify pass
    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--hunks", "math.ts:1"])
    report_pass = atomic_commit.verify_staged_slice(temp_git_repo)
    assert report_pass["passed"] is True


def test_verify_ephemeral_worktree_command_execution(temp_git_repo: Path):
    """Verifies commands run in an isolated ephemeral worktree containing only HEAD + staged index."""
    cfg = temp_git_repo / "config.json"
    cfg.write_text('{"ready": true}\n', encoding="utf-8")
    runner = temp_git_repo / "check_cfg.py"
    runner.write_text(
        "import json, pathlib, sys\n"
        "p = pathlib.Path('config.json')\n"
        "sys.exit(0 if p.exists() and json.loads(p.read_text()).get('ready') else 1)\n",
        encoding="utf-8",
    )
    untracked_note = temp_git_repo / "notes.txt"
    untracked_note.write_text("keep me untouched in worktree\n", encoding="utf-8")

    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", "--files", "check_cfg.py"])

    report_fail = atomic_commit.verify_staged_slice(
        temp_git_repo, commands=["python3 check_cfg.py"]
    )
    assert report_fail["passed"] is False
    assert report_fail["command_results"][0]["returncode"] == 1

    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--files", "config.json"])
    report_pass = atomic_commit.verify_staged_slice(
        temp_git_repo, commands=["python3 check_cfg.py"]
    )
    assert report_pass["passed"] is True
    assert report_pass["command_results"][0]["returncode"] == 0
    assert untracked_note.read_text(encoding="utf-8") == "keep me untouched in worktree\n"


def test_tsconfig_paths_extends_and_jsonc_import_resolution(temp_git_repo: Path):
    """Verifies inspect_status and verify_staged_slice follow tsconfig extends + JSONC path aliases."""
    (temp_git_repo / "tsconfig.base.json").write_text(
        '{\n'
        '  // Base config with trailing comma\n'
        '  "compilerOptions": {\n'
        '    "baseUrl": ".",\n'
        '    "paths": {\n'
        '      "@shared/*": ["shared/src/*"],\n'
        '    },\n'
        '  }\n'
        '}\n',
        encoding="utf-8",
    )
    (temp_git_repo / "client").mkdir()
    (temp_git_repo / "client/tsconfig.json").write_text(
        '{\n  "extends": "../tsconfig.base.json",\n  "compilerOptions": {"paths": {"@ui/*": ["client/src/*"]}}\n}\n',
        encoding="utf-8",
    )
    (temp_git_repo / "shared/src").mkdir(parents=True)
    (temp_git_repo / "shared/src/types.ts").write_text("export interface Unit { id: string; }\n", encoding="utf-8")
    (temp_git_repo / "client/src").mkdir(parents=True)
    (temp_git_repo / "client/src/view.ts").write_text(
        "import type { Unit } from '@shared/types';\nexport const name = 'ok';\n",
        encoding="utf-8",
    )

    status = atomic_commit.inspect_status(temp_git_repo)
    assert "shared/src/types.ts" in status["topological_waves"][0]
    assert status["topological_waves"][1] == ["client/src/view.ts"]

    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", "--files", "client/src/view.ts"])
    blocked = atomic_commit.verify_staged_slice(temp_git_repo, skip_worktree=True)
    assert blocked["passed"] is False
    assert "shared/src/types.ts" in "\n".join(blocked["errors"])

    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--files", "shared/src/types.ts"])
    passed = atomic_commit.verify_staged_slice(temp_git_repo, skip_worktree=True)
    assert passed["passed"] is True


def test_verify_symlinks_subpackage_node_modules_and_enforces_timeout(temp_git_repo: Path):
    """Verifies ephemeral worktree links <subdir>/node_modules and enforces command timeouts."""
    pkg = temp_git_repo / "client"
    (pkg / "node_modules" / "fake-dep").mkdir(parents=True)
    (pkg / "node_modules" / "fake-dep" / "marker.txt").write_text("cached\n", encoding="utf-8")
    probe = pkg / "probe.py"
    probe.write_text(
        "import pathlib, sys\n"
        "sys.exit(0 if pathlib.Path('client/node_modules/fake-dep/marker.txt').exists() else 1)\n",
        encoding="utf-8",
    )
    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", "--files", "client/probe.py"])

    ok_res = atomic_commit.verify_staged_slice(temp_git_repo, commands=["python3 client/probe.py"])
    assert ok_res["passed"] is True

    timeout_res = atomic_commit.verify_staged_slice(
        temp_git_repo,
        commands=["python3 -c 'import time; time.sleep(2)'"],
        command_timeout=1,
    )
    assert timeout_res["passed"] is False
    assert timeout_res["command_results"][0]["returncode"] == 124
    assert "timed out" in "\n".join(timeout_res["errors"])


def test_status_and_stage_handle_spaces_and_unicode_filenames(temp_git_repo: Path):
    """Verifies -z NUL-delimited Git parsing handles spaces and non-ASCII paths cleanly."""
    spaced = temp_git_repo / "assets/hero sprite.ts"
    spaced.parent.mkdir(parents=True)
    spaced.write_text("export const x = 1;\n", encoding="utf-8")
    status = atomic_commit.inspect_status(temp_git_repo)
    paths = {f["path"] for f in status["files"]}
    assert "assets/hero sprite.ts" in paths
    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", "--files", "assets/hero sprite.ts"])
    assert "assets/hero sprite.ts" in atomic_commit.get_staged_files(temp_git_repo)


@pytest.mark.parametrize(
    ("rel_path", "initial_text", "updated_text", "cached", "expected_role", "expected_logic", "expected_comment"),
    [
        pytest.param(
            "mod.py",
            'def fn():\n    """start\n    old doc\n    """\n    return 1\n',
            'def fn():\n    """start\n    new doc\n    """\n    return 1\n',
            False,
            "production",
            0,
            2,
            id="worktree_docstring_comment_only",
        ),
        pytest.param(
            "uv.lock",
            "v = 1\n",
            "v = 2\n",
            True,
            "generated",
            0,
            0,
            id="staged_generated_lockfile",
        ),
    ],
)
def test_get_file_hunks_and_diff_stats_full_blob(
    temp_git_repo: Path,
    rel_path: str,
    initial_text: str,
    updated_text: str,
    cached: bool,
    expected_role: str,
    expected_logic: int,
    expected_comment: int,
):
    """Verifies get_file_hunks_with_stats and _compute_file_diff_stats across worktree and cached diffs."""
    target = temp_git_repo / rel_path
    target.write_text(initial_text, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=temp_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", f"add {rel_path}"], cwd=temp_git_repo, check=True, capture_output=True)

    target.write_text(updated_text, encoding="utf-8")
    if cached:
        subprocess.run(["git", "add", rel_path], cwd=temp_git_repo, check=True)

    _, hunk_dicts = atomic_commit.get_file_hunks_with_stats(temp_git_repo, rel_path, cached=cached)
    assert len(hunk_dicts) == 1
    assert hunk_dicts[0]["logic_lines"] == expected_logic
    assert hunk_dicts[0]["comment_lines"] == expected_comment

    stats = atomic_commit._compute_file_diff_stats(
        temp_git_repo, rel_path, cached=cached, base_ref="HEAD" if cached else None
    )
    assert stats.file_role == expected_role
    assert stats.logic_lines == expected_logic
    assert stats.comment_lines == expected_comment


@pytest.mark.parametrize(
    ("count_tests", "count_comments", "expected_effective_lines"),
    [
        pytest.param(False, False, 3, id="default_logic_only"),
        pytest.param(True, False, 7, id="include_tests"),
        pytest.param(False, True, 11, id="include_prod_comments"),
        pytest.param(True, True, 17, id="include_tests_and_comments"),
    ],
)
def test_line_counting_skips_comments_tests_blanks_and_generated_by_default(
    temp_git_repo: Path,
    count_tests: bool,
    count_comments: bool,
    expected_effective_lines: int,
):
    """Verifies line classification excludes tests, comments/docstrings, blanks, and lockfiles by default."""
    files = {
        "src/engine.py": (
            '"""Engine module.\n'
            "Doc line 2.\n"
            '"""\n'
            "\n"
            "# compute tick\n"
            "def tick(x: int) -> int:\n"
            "    return x + 1\n"
        ),
        "client/view.ts": (
            "/**\n"
            " * Render view.\n"
            " */\n"
            "// single line\n"
            "\n"
            "export const view = 1;\n"
        ),
        "tests/test_engine.py": (
            "# test helper comment\n"
            "\n"
            "def test_tick():\n"
            "    assert True\n"
        ),
        "client/view.test.ts": (
            "// test spec\n"
            "it('ok', () => {\n"
            "});\n"
        ),
        "uv.lock": "version = 1\n" * 5,
    }
    for rel_path, content in files.items():
        p = temp_git_repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    report = atomic_commit.inspect_status(
        temp_git_repo,
        count_tests=count_tests,
        count_comments=count_comments,
    )
    ss = report["size_summary"]
    assert ss["effective_lines"] == expected_effective_lines
    assert ss["logic_lines"] == 3
    assert ss["test_lines"] == 4
    assert ss["comment_lines"] == 10
    assert ss["blank_lines"] == 3
    assert ss["generated_lines"] == 5

    by_path = {f["path"]: f for f in report["files"]}
    assert by_path["src/engine.py"]["file_role"] == "production"
    assert by_path["tests/test_engine.py"]["file_role"] == "test"
    assert by_path["client/view.test.ts"]["file_role"] == "test"
    assert by_path["uv.lock"]["file_role"] == "generated"
    assert by_path["uv.lock"]["effective_lines"] == 0


@pytest.mark.parametrize(
    ("max_lines", "max_files", "expected_warn_substr"),
    [
        pytest.param(10, 10, None, id="within_budget"),
        pytest.param(1, 10, "Commit size warning", id="exceeds_line_budget"),
        pytest.param(10, 0, None, id="unlimited_files_when_zero"),
    ],
)
def test_compute_staged_slice_metrics(
    temp_git_repo: Path,
    max_lines: int,
    max_files: int,
    expected_warn_substr: str | None,
):
    """Verifies _compute_staged_slice_metrics totals, pairing metadata, and size warnings."""
    (temp_git_repo / "app.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    (temp_git_repo / "tests").mkdir(exist_ok=True)
    (temp_git_repo / "tests/test_app.py").write_text("def test_a(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py", "tests/test_app.py"], cwd=temp_git_repo, check=True)
    staged = atomic_commit.get_staged_files(temp_git_repo)
    cfg = atomic_commit.SizingConfig(max_commit_lines=max_lines, max_commit_files=max_files)
    metrics, warns, prod_paths, exec_lines, exec_files = atomic_commit._compute_staged_slice_metrics(
        temp_git_repo, staged, cfg, head_exists=True
    )
    assert metrics["effective_lines"] == 2
    assert prod_paths == ["app.py"]
    assert exec_lines == 2 and exec_files == ["app.py"]
    if expected_warn_substr is None:
        assert warns == []
    else:
        assert any(expected_warn_substr in w for w in warns)


@pytest.mark.parametrize(
    ("count_comments", "expected_effective_lines"),
    [
        pytest.param(False, 0, id="exclude_comments_default"),
        pytest.param(True, 4, id="include_comments_override"),
    ],
)
def test_mid_docstring_edit_classified_as_comment_via_full_blob(
    temp_git_repo: Path,
    count_comments: bool,
    expected_effective_lines: int,
):
    """Verifies edits deep inside a multi-line docstring/block comment are classified as comments, not logic."""
    py_path = temp_git_repo / "documented.py"
    lines = [
        "def documented():\n",
        '    """Start of a long docstring.\n',
        *[f"    Doc line {i}.\n" for i in range(1, 14)],
        '    """\n',
        "    return 42\n",
    ]
    py_path.write_text("".join(lines), encoding="utf-8")
    subprocess.run(["git", "add", "documented.py"], cwd=temp_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "docs: add documented.py"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    lines[8] = "    Updated middle documentation line 7.\n"
    lines[9] = "    Updated middle documentation line 8.\n"
    py_path.write_text("".join(lines), encoding="utf-8")

    status_report = atomic_commit.inspect_status(temp_git_repo, count_comments=count_comments)
    fentry = status_report["files"][0]
    assert fentry["logic_lines"] == 0
    assert fentry["comment_lines"] == 4
    assert fentry["effective_lines"] == expected_effective_lines

    _, hunk_dicts = atomic_commit.get_file_hunks_with_stats(temp_git_repo, "documented.py")
    assert len(hunk_dicts) == 1
    assert hunk_dicts[0]["logic_lines"] == 0
    assert hunk_dicts[0]["comment_lines"] == 4

    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", "--files", "documented.py"])
    res = atomic_commit.verify_staged_slice(
        temp_git_repo, skip_worktree=True, count_comments=count_comments
    )
    assert res["passed"] is True
    assert res["size_metrics"]["logic_lines"] == 0
    assert res["size_metrics"]["comment_lines"] == 4
    assert res["size_metrics"]["effective_lines"] == expected_effective_lines


@pytest.mark.parametrize(
    ("staged_files", "max_lines", "max_files", "expected_eff_lines", "expected_warn_substr"),
    [
        pytest.param(["tests/test_big_module.py", "uv.lock"], 20, 10, 0, None, id="tests_and_lockfile_only"),
        pytest.param(
            ["src/big_module.py", "tests/test_big_module.py"],
            20,
            10,
            25,
            "Commit size warning",
            id="oversized_production_slice",
        ),
        pytest.param(
            ["src/p1.py", "src/p2.py", "src/p3.py", "tests/test_big_module.py", "uv.lock"],
            200,
            2,
            3,
            "Commit scope warning",
            id="scope_exceeds_max_files",
        ),
    ],
)
def test_verify_emits_non_blocking_size_and_file_warnings(
    temp_git_repo: Path,
    capsys,
    staged_files: list[str],
    max_lines: int,
    max_files: int,
    expected_eff_lines: int,
    expected_warn_substr: str | None,
):
    """Verifies size and file-count thresholds emit warnings without blocking verify (passed stays True)."""
    (temp_git_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_git_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_git_repo / "src/big_module.py").write_text(
        "".join(f"x_{i} = {i}\n" for i in range(25)), encoding="utf-8"
    )
    (temp_git_repo / "tests/test_big_module.py").write_text(
        "".join(f"def test_{i}(): assert True\n" for i in range(30)), encoding="utf-8"
    )
    (temp_git_repo / "uv.lock").write_text("".join(f"pkg-{i}\n" for i in range(50)), encoding="utf-8")
    for idx in range(1, 4):
        (temp_git_repo / f"src/p{idx}.py").write_text(f"val = {idx}\n", encoding="utf-8")

    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", "--files", *staged_files])
    res = atomic_commit.verify_staged_slice(
        temp_git_repo, skip_worktree=True, max_commit_lines=max_lines, max_commit_files=max_files
    )
    assert res["passed"] is True
    assert res["errors"] == []
    assert res["size_metrics"]["effective_lines"] == expected_eff_lines
    if expected_warn_substr is None:
        assert res["warnings"] == []
    else:
        assert any(expected_warn_substr in w for w in res["warnings"])
        capsys.readouterr()
        rc = atomic_commit.main(
            [
                "--repo",
                str(temp_git_repo),
                "verify",
                "--skip-worktree",
                "--max-commit-lines",
                str(max_lines),
                "--max-commit-files",
                str(max_files),
            ]
        )
        assert rc == 0
        assert expected_warn_substr in capsys.readouterr().out


def test_status_recommends_stacked_prs_when_exceeding_max_pr_lines(temp_git_repo: Path, capsys):
    """Verifies inspect_status and CLI flags report recommended_min_commits, recommended_pr_count, and PR warnings."""
    mod_a = temp_git_repo / "mod_a.py"
    base_a = [f"a_{i} = {i}\n" for i in range(1, 40)]
    mod_a.write_text("".join(base_a), encoding="utf-8")
    subprocess.run(["git", "add", "mod_a.py"], cwd=temp_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: add mod_a"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # Create 2 separate hunks totaling 24 logic lines in mod_a.py
    for idx in range(0, 12):
        base_a[idx] = f"a_{idx + 1} = {idx + 100}\n"
    for idx in range(26, 38):
        base_a[idx] = f"a_{idx + 1} = {idx + 200}\n"
    mod_a.write_text("".join(base_a), encoding="utf-8")

    # Add untracked mod_b.py with 20 logic lines (44 lines across 2 modified+new files? 12*2+12*2? Wait: 12 removed + 12 added per hunk!)
    mod_b = temp_git_repo / "mod_b.py"
    mod_b.write_text("".join(f"b_{i} = {i}\n" for i in range(12)), encoding="utf-8")

    report = atomic_commit.inspect_status(temp_git_repo, max_commit_lines=20, max_pr_lines=30)
    ss = report["size_summary"]
    assert ss["effective_lines"] == 60  # 48 churn in mod_a + 12 in mod_b
    assert ss["recommended_min_commits"] == 3
    assert ss["recommended_pr_count"] == 2
    assert any("PR size warning" in w and "stacked PRs" in w for w in ss["warnings"])
    assert any("mod_a.py" in w and "hunks" in w for w in ss["warnings"])

    capsys.readouterr()
    rc = atomic_commit.main(
        [
            "--repo",
            str(temp_git_repo),
            "status",
            "--max-commit-lines",
            "20",
            "--max-pr-lines",
            "30",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Size Summary:" in out
    assert "PR size warning" in out


@pytest.mark.parametrize(
    ("extra_files", "staged_files", "skip_worktree", "expected_cov_status", "expected_warn_substr"),
    [
        pytest.param({}, ["docs/notes.md"], True, "skipped", None, id="docs_only"),
        pytest.param(
            {}, ["src/logic.py"], True, "skipped", "Test pairing warning", id="untested_executable_warns"
        ),
        pytest.param(
            {".crenet-governance.json": '{"coverage": false}\n'},
            ["src/logic.py", ".crenet-governance.json"],
            False,
            "opted_out",
            None,
            id="governance_opt_out",
        ),
    ],
)
def test_test_pairing_warning_and_opt_out(
    temp_git_repo: Path,
    extra_files: dict[str, str],
    staged_files: list[str],
    skip_worktree: bool,
    expected_cov_status: str,
    expected_warn_substr: str | None,
):
    """Verifies Test pairing warning fires on untested executable slices, skips docs, and respects opt-out."""
    (temp_git_repo / "src").mkdir()
    (temp_git_repo / "src/logic.py").write_text("def compute(x):\n    return x * 2\n", encoding="utf-8")
    (temp_git_repo / "docs").mkdir()
    (temp_git_repo / "docs/notes.md").write_text("# Architecture\n\nDetails.\n", encoding="utf-8")
    for rel_path, content in extra_files.items():
        (temp_git_repo / rel_path).write_text(content, encoding="utf-8")

    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", "--files", *staged_files])
    res = atomic_commit.verify_staged_slice(temp_git_repo, skip_worktree=skip_worktree)
    assert res["passed"] is True
    assert res["coverage"]["status"] == expected_cov_status
    if expected_warn_substr is None:
        assert res["warnings"] == []
    else:
        assert any(expected_warn_substr in w for w in res["warnings"])


def _setup_calc_ratchet_repo(repo: Path) -> None:
    import json as _json

    (repo / "src").mkdir()
    (repo / "src/calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def dead_covered_a():\n"
        "    x = 1\n"
        "    return x\n"
        "\n"
        "def legacy_untested():\n"
        "    return 99\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests/test_calc.py").write_text(
        "from src.calc import add, dead_covered_a\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "    assert dead_covered_a() == 1\n",
        encoding="utf-8",
    )
    cov_cfg_payload = {
        "coverage": {
            "command": (
                f"{sys.executable} -m coverage run --source=src -m pytest -q "
                f"&& {sys.executable} -m coverage json -o coverage.json"
            ),
            "report_path": "coverage.json",
            "mode": "warn",
        }
    }
    (repo / ".crenet-governance.json").write_text(
        _json.dumps(cov_cfg_payload, indent=2) + "\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: add calc with coverage config"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", head_sha],
        cwd=repo,
        check=True,
    )


@pytest.mark.parametrize(
    (
        "calc_src",
        "test_src",
        "staged_files",
        "coverage_mode",
        "expected_passed",
        "expected_status",
        "expected_delta_missed",
    ),
    [
        pytest.param(
            (
                "def add(a, b):\n    return a + b\n\n"
                "def dead_covered_a():\n    x = 1\n    return x\n\n"
                "def legacy_untested():\n    return 99\n\n"
                "def brand_new_untested(z):\n    y = z + 1\n    return y\n"
            ),
            None,
            ["src/calc.py"],
            None,
            True,
            "regressed",
            2,
            id="untested_addition_warn",
        ),
        pytest.param(
            (
                "def add(a, b):\n    return a + b\n\n"
                "def dead_covered_a():\n    x = 1\n    return x\n\n"
                "def legacy_untested():\n    return 99\n\n"
                "def brand_new_untested(z):\n    y = z + 1\n    return y\n"
            ),
            None,
            ["src/calc.py"],
            "error",
            False,
            "regressed",
            2,
            id="untested_addition_error",
        ),
        pytest.param(
            "def add(a, b):\n    return a + b\n\n" "def legacy_untested():\n    return 99\n",
            "from src.calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
            ["src/calc.py", "tests/test_calc.py"],
            "error",
            True,
            "evaluated",
            0,
            id="dead_covered_deletion_no_false_positive",
        ),
    ],
)
def test_verify_coverage_ratchet_cold_start_warn_error_and_dead_code_deletion(
    temp_git_repo: Path,
    tmp_path: Path,
    calc_src: str,
    test_src: str | None,
    staged_files: list[str],
    coverage_mode: str | None,
    expected_passed: bool,
    expected_status: str,
    expected_delta_missed: int,
):
    """Verifies cold-start HEAD/origin baseline capture, warn vs error mode, and zero false positive on dead-code deletion."""
    _setup_calc_ratchet_repo(temp_git_repo)
    (temp_git_repo / "src/calc.py").write_text(calc_src, encoding="utf-8")
    if test_src is not None:
        (temp_git_repo / "tests/test_calc.py").write_text(test_src, encoding="utf-8")

    atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", "--files", *staged_files])
    res = atomic_commit.verify_staged_slice(
        temp_git_repo,
        coverage_mode=coverage_mode,
        cache_path_override=tmp_path / "ratchet-cache.json",
    )
    assert res["passed"] is expected_passed
    assert res["coverage"]["status"] == expected_status
    assert res["coverage"]["vs_head"]["delta_missed"] == expected_delta_missed
    assert res["coverage"]["vs_upstream"]["anchor_ref"] == "origin/main"
    if not expected_passed:
        assert any("Coverage regression (error)" in e for e in res["errors"])
    elif expected_delta_missed > 0:
        assert any("Coverage regression (warn)" in w and "src/calc.py (+2 missed)" in w for w in res["warnings"])
    else:
        assert res["coverage"]["vs_head"]["delta_pct"] < 0
        assert res["coverage"]["vs_head"]["regressed"] is False
        assert res["warnings"] == []


@pytest.mark.parametrize(
    ("cli_max_lines", "expect_size_warning"),
    [
        pytest.param(None, True, id="config_threshold_warns"),
        pytest.param(50, False, id="cli_override_passes_cleanly"),
    ],
)
def test_governance_sizing_config_integration_and_cli_override(
    temp_git_repo: Path,
    cli_max_lines: int | None,
    expect_size_warning: bool,
):
    """Verifies `.crenet-governance.json` sizing rules drive status/verify and explicit flags override."""
    import json as _json

    (temp_git_repo / "src/generated").mkdir(parents=True)
    (temp_git_repo / "src/proto").mkdir(parents=True)
    (temp_git_repo / "tests/fixtures").mkdir(parents=True)
    (temp_git_repo / "src/domain").mkdir(parents=True)

    (temp_git_repo / "src/generated/routes.ts").write_text(
        "".join(f"export const r{i} = {i};\n" for i in range(40)), encoding="utf-8"
    )
    (temp_git_repo / "src/proto/msg.pb.ts").write_text(
        "".join(f"export const p{i} = {i};\n" for i in range(30)), encoding="utf-8"
    )
    (temp_git_repo / "tests/fixtures/seed.ts").write_text(
        "".join(f"export const f{i} = {i};\n" for i in range(25)), encoding="utf-8"
    )
    (temp_git_repo / "src/domain/service.py").write_text(
        "".join(f"s_{i} = {i}\n" for i in range(12)), encoding="utf-8"
    )
    (temp_git_repo / ".crenet-governance.json").write_text(
        _json.dumps(
            {
                "coverage": False,
                "sizing": {
                    "max_commit_lines": 10,
                    "max_pr_lines": 25,
                    "generated_extensions": [".pb.ts"],
                    "generated_globs": ["src/generated/**"],
                    "test_globs": ["**/fixtures/**"],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    status = atomic_commit.inspect_status(temp_git_repo)
    by_path = {f["path"]: f for f in status["files"]}
    assert by_path["src/generated/routes.ts"]["file_role"] == "generated"
    assert by_path["src/generated/routes.ts"]["effective_lines"] == 0
    assert by_path["src/proto/msg.pb.ts"]["file_role"] == "generated"
    assert by_path["tests/fixtures/seed.ts"]["file_role"] == "test"
    assert status["size_summary"]["max_commit_lines"] == 10
    assert status["size_summary"]["effective_lines"] == 28

    atomic_commit.main(
        [
            "--repo",
            str(temp_git_repo),
            "stage",
            "--reset",
            "--files",
            ".crenet-governance.json",
            "src/generated/routes.ts",
            "src/proto/msg.pb.ts",
            "tests/fixtures/seed.ts",
            "src/domain/service.py",
        ]
    )

    cli_args = ["--repo", str(temp_git_repo), "verify", "--skip-worktree"]
    if cli_max_lines is not None:
        cli_args.extend(["--max-commit-lines", str(cli_max_lines)])
    assert atomic_commit.main(cli_args) == 0

    res = atomic_commit.verify_staged_slice(
        temp_git_repo, skip_worktree=True, max_commit_lines=cli_max_lines
    )
    assert res["passed"] is True
    assert res["size_metrics"]["generated_files"] == 2
    assert res["size_metrics"]["test_files"] == 1
    assert res["size_metrics"]["max_commit_lines"] == (cli_max_lines or 10)
    assert bool(any("Commit size warning" in w for w in res["warnings"])) is expect_size_warning


def test_cli_chunks_and_hunks_suggest_line_slices(temp_git_repo: Path, capsys):
    """Verifies CLI chunks --json and hunks surface top-level symbol chunks and suggested `--lines` slices."""
    import json as _json

    (temp_git_repo / ".crenet-governance.json").write_text('{"coverage": false}\n', encoding="utf-8")
    subprocess.run(["git", "add", ".crenet-governance.json"], cwd=temp_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "opt out cov"], cwd=temp_git_repo, check=True, capture_output=True)

    (temp_git_repo / "svc.py").write_text(
        "def _foundation(n: int) -> int:\n    return n + 1\n\n"
        "def high_level(n: int) -> int:\n    return _foundation(n) * 2\n",
        encoding="utf-8",
    )

    capsys.readouterr()
    assert atomic_commit.main(["--repo", str(temp_git_repo), "chunks", "svc.py", "--json"]) == 0
    chunks_json = _json.loads(capsys.readouterr().out)
    assert chunks_json["chunk_count"] == 2
    assert len(chunks_json["suggested_line_slices"]) >= 1

    assert atomic_commit.main(["--repo", str(temp_git_repo), "hunks", "svc.py"]) == 0
    assert "Suggested `--lines` Slices:" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("stage_args", "expected_passed", "expected_err_substr"),
    [
        pytest.param(["--lines", "svc.py:3-5"], False, "symbol '_foundation'", id="missing_intrafile_dep_fails"),
        pytest.param(
            ["--lines", "svc.py:1-2", "--lines", "svc.py:1-2"],
            True,
            None,
            id="bottom_up_and_repeated_flags_pass",
        ),
    ],
)
def test_cli_stage_lines_and_intrafile_verify(
    temp_git_repo: Path,
    stage_args: list[str],
    expected_passed: bool,
    expected_err_substr: str | None,
):
    """Verifies CLI `stage --lines` supports partial slices and verify flags missing intra-file symbols."""
    (temp_git_repo / ".crenet-governance.json").write_text('{"coverage": false}\n', encoding="utf-8")
    subprocess.run(["git", "add", ".crenet-governance.json"], cwd=temp_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "opt out cov"], cwd=temp_git_repo, check=True, capture_output=True)

    (temp_git_repo / "svc.py").write_text(
        "def _foundation(n: int) -> int:\n    return n + 1\n\n"
        "def high_level(n: int) -> int:\n    return _foundation(n) * 2\n",
        encoding="utf-8",
    )

    assert atomic_commit.main(["--repo", str(temp_git_repo), "stage", "--reset", *stage_args]) == 0
    res = atomic_commit.verify_staged_slice(temp_git_repo, skip_worktree=True)
    assert res["passed"] is expected_passed
    if expected_err_substr is None:
        assert res["errors"] == []
    else:
        assert any(expected_err_substr in e for e in res["errors"])


@pytest.mark.parametrize(
    ("argv", "expected_attrs"),
    [
        pytest.param(
            ["status", "--include-tests", "--include-comments", "--max-commit-lines", "50", "--max-pr-lines", "120"],
            {"subcommand": "status", "include_tests": True, "include_comments": True, "max_commit_lines": 50, "max_pr_lines": 120},
            id="status_sizing_flags",
        ),
        pytest.param(
            ["chunks", "app.py", "--max-chunk-lines", "80", "--json"],
            {"subcommand": "chunks", "file": "app.py", "max_chunk_lines": 80, "json": True},
            id="chunks_subcommand",
        ),
        pytest.param(
            ["stage", "--lines", "a.py:1-5", "--lines", "b.py:2-4"],
            {"subcommand": "stage", "lines": ["a.py:1-5", "b.py:2-4"]},
            id="stage_repeated_lines",
        ),
        pytest.param(
            ["verify", "--coverage-cmd", "pytest", "--coverage-report", "cov.json", "--coverage-mode", "error"],
            {"subcommand": "verify", "coverage_cmd": "pytest", "coverage_report": ["cov.json"], "coverage_mode": "error"},
            id="verify_coverage_flags",
        ),
    ],
)
def test_build_parser_new_flags_and_chunks(argv: list[str], expected_attrs: dict):
    """Verifies build_parser registers chunks subcommand and sizing/lines/coverage flags."""
    ns = atomic_commit.build_parser().parse_args(argv)
    for key, val in expected_attrs.items():
        assert getattr(ns, key) == val
