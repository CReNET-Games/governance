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

