"""Parameterized unit tests for scripts/commit_sizing.py."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

import commit_sizing  # noqa: E402


@dataclass
class _StubHunk:
    index: int
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section_hint: str
    body_lines: list[str]
    preview: str = ""


@pytest.mark.parametrize(
    ("rel_path", "expected_role"),
    [
        ("src/engine.py", "production"),
        ("client/view.ts", "production"),
        ("tests/test_engine.py", "test"),
        ("client/view.test.ts", "test"),
        ("pkg/foo.spec.tsx", "test"),
        ("uv.lock", "generated"),
        ("coverage/coverage-summary.json", "generated"),
        ("public/bundle.min.js", "generated"),
        ("assets/hero.png", "generated"),
    ],
)
def test_classify_file_role(rel_path: str, expected_role: str):
    """Verifies production, test, and generated/lockfile path classification."""
    assert commit_sizing.classify_file_role(rel_path) == expected_role


@pytest.mark.parametrize(
    ("count_tests", "count_comments", "expected_effective_lines"),
    [
        pytest.param(False, False, 3, id="default_logic_only"),
        pytest.param(True, False, 7, id="include_tests"),
        pytest.param(False, True, 11, id="include_prod_comments"),
        pytest.param(True, True, 17, id="include_tests_and_comments"),
    ],
)
def test_full_text_line_counting_skips_comments_tests_blanks_and_generated(
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
    agg = commit_sizing.LineStats()
    total_eff = 0
    for rel_path, content in files.items():
        st = commit_sizing._stats_from_full_text(rel_path, content)
        agg.add_other(st)
        total_eff += st.effective_lines(count_tests=count_tests, count_comments=count_comments)

    assert total_eff == expected_effective_lines
    assert agg.logic_lines == 3
    assert agg.test_lines == 4
    assert agg.comment_lines == 10
    assert agg.blank_lines == 3
    assert agg.generated_lines == 5


def test_hunk_stats_with_full_blob_context_and_html_hash_comments():
    """Verifies hunk line stats resolve mid-docstring, HTML comment, and Makefile comment lines."""
    py_lines = [
        "def documented():",
        '    """Start of docstring.',
        "    Middle doc line.",
        '    """',
        "    return 42",
    ]
    cls_sets = commit_sizing.classify_source_lines("mod.py", py_lines)
    hunk = _StubHunk(
        index=1,
        header="@@ -2,2 +2,2 @@",
        old_start=2,
        old_count=2,
        new_start=2,
        new_count=2,
        section_hint="def documented():",
        body_lines=["-    Old doc.", "+    Middle doc line."],
    )
    hd = commit_sizing.compute_hunk_dict(
        "mod.py", hunk, old_classified=cls_sets, new_classified=cls_sets
    )
    assert hd["logic_lines"] == 0
    assert hd["comment_lines"] == 2

    html_blank, html_comment = commit_sizing.classify_source_lines(
        "index.html", ["<!-- header", "banner -->", "<main>ok</main>"]
    )
    assert html_blank == set()
    assert html_comment == {1, 2}

    mk_blank, mk_comment = commit_sizing.classify_source_lines(
        "Makefile", ["# build rule", "", "test:\n"]
    )
    assert mk_blank == {2}
    assert mk_comment == {1}


@pytest.mark.parametrize(
    ("raw_line", "in_block", "fallback_mode", "expected"),
    [
        pytest.param(" * fallback bullet", False, True, (True, False), id="fallback_star"),
        pytest.param(" */", False, True, (True, False), id="fallback_close_block"),
        pytest.param(" */ // end", False, True, (True, False), id="fallback_close_with_comment"),
        pytest.param(
            'const s = "a\\"b"; /* open block',
            False,
            False,
            (False, True),
            id="escaped_quote_before_block",
        ),
    ],
)
def test_c_style_and_unescaped_comment_edge_cases(
    raw_line: str, in_block: bool, fallback_mode: bool, expected: tuple[bool, bool]
):
    """Verifies escaped delimiter counting and fallback C-style comment heuristics."""
    assert commit_sizing._process_c_style_line(
        raw_line, in_block, fallback_mode=fallback_mode
    ) == expected
    assert commit_sizing._count_unescaped('pre\\"\\"\\"post"""', '"""') == 1


@pytest.mark.parametrize(
    ("rel_path", "lines", "expected_blank", "expected_comment"),
    [
        pytest.param(
            "engine.py",
            ["#!/usr/bin/env python3", "# header", '"""doc"""', "", "x = 1"],
            {4},
            {2, 3},
            id="python_basic",
        ),
        pytest.param(
            "doc.py",
            ['"""escaped \\""" inside', 'still doc"""'],
            set(),
            {1, 2},
            id="python_escaped_docstring",
        ),
        pytest.param(
            "view.ts",
            ["/**", " * doc", " */", "// note", "", "export const v = 1;"],
            {5},
            {1, 2, 3, 4},
            id="typescript_block_and_line",
        ),
        pytest.param(
            "index.html",
            ["<!-- start", "middle", "end -->", "  <div>body</div>", "  <!-- note -->"],
            set(),
            {1, 2, 3, 5},
            id="html_block_and_inline",
        ),
        pytest.param("Makefile", ["# build rule", "", "test:"], {2}, {1}, id="makefile_hash"),
    ],
)
def test_classify_source_lines_languages(
    rel_path: str, lines: list[str], expected_blank: set[int], expected_comment: set[int]
):
    """Verifies classify_source_lines across Python, TypeScript, HTML, and Makefile inputs."""
    assert commit_sizing.classify_source_lines(rel_path, lines) == (
        expected_blank,
        expected_comment,
    )


@pytest.mark.parametrize(
    ("rel_path", "body_lines", "expected"),
    [
        pytest.param(
            "app.ts",
            [
                " const keep = 1;",
                "-// old comment",
                "-const oldVal = 1;",
                "+ * mid doc bullet",
                "+ */",
                "+const newVal = 2;",
            ],
            commit_sizing.LineStats(
                file_role="production",
                raw_additions=3,
                raw_deletions=2,
                logic_lines=2,
                comment_lines=3,
                generated_lines=0,
            ),
            id="fallback_c_style_and_context",
        ),
        pytest.param(
            "uv.lock",
            ["-v1", "+v2", "+v3"],
            commit_sizing.LineStats(
                file_role="generated",
                raw_additions=2,
                raw_deletions=1,
                logic_lines=0,
                comment_lines=0,
                generated_lines=3,
            ),
            id="generated_lockfile",
        ),
    ],
)
def test_hunk_stats_fallback_generated_and_context_lines(
    rel_path: str, body_lines: list[str], expected: commit_sizing.LineStats
):
    """Verifies fallback comment classification, generated files, and raw/role stats in hunks."""
    hunk = _StubHunk(
        index=1,
        header="@@ -1,1 +1,1 @@",
        old_start=1,
        old_count=1,
        new_start=1,
        new_count=1,
        section_hint="",
        body_lines=body_lines,
    )
    assert commit_sizing.compute_hunk_line_stats(rel_path, hunk) == expected


def test_stats_from_full_text_none_returns_empty():
    """Verifies _stats_from_full_text returns zero effective lines when text is None."""
    assert commit_sizing._stats_from_full_text("missing.py", None).effective_lines() == 0


def test_read_text_safe_handles_missing_binary_non_utf8_and_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verifies _read_text_safe returns None on missing, binary, non-UTF-8, or OSError paths."""
    good = tmp_path / "good.py"
    good.write_text("x = 1\n", encoding="utf-8")
    assert commit_sizing._read_text_safe(good) == "x = 1\n"

    assert commit_sizing._read_text_safe(tmp_path / "does_not_exist.py") is None

    nul_file = tmp_path / "nul.bin"
    nul_file.write_bytes(b"abc\x00def")
    assert commit_sizing._read_text_safe(nul_file) is None

    bad_utf8 = tmp_path / "bad.txt"
    bad_utf8.write_bytes(b"\xff\xfe\xfd")
    assert commit_sizing._read_text_safe(bad_utf8) is None

    def _raise_oserror(_self: Path) -> bytes:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_bytes", _raise_oserror)
    assert commit_sizing._read_text_safe(good) is None


@pytest.mark.parametrize(
    ("rel_path", "pattern", "expected"),
    [
        ("src/generated/client.ts", "src/generated/**", True),
        ("src/other/client.ts", "src/generated/**", False),
        ("packages/api/types.gen.ts", "**/*.gen.ts", True),
        ("types.gen.ts", "**/*.gen.ts", True),
        ("tests/fixtures/seed.json", "**/fixtures/**", True),
        ("protos/v1/unit.proto", "*.proto", True),
        ("docs/v2.txt", "./v?.txt", True),
        ("src/app.py", "", False),
    ],
)
def test_matches_repo_glob_patterns(rel_path: str, pattern: str, expected: bool):
    """Verifies repo-relative glob matching supports **, *, ?, leading ./, and basenames."""
    assert commit_sizing._matches_repo_glob(rel_path, pattern) is expected


@pytest.mark.parametrize(
    ("rel_path", "expected_role"),
    [
        ("src/proto/msg.pb.ts", "generated"),
        ("lib/model.g.dart", "generated"),
        ("./src/generated/routes.ts", "generated"),
        ("nested/types.gen.ts", "generated"),
        ("schema.graphql", "generated"),
        ("tests/fixtures/world.ts", "test"),
        ("bench/tick.bench.ts", "test"),
        ("src/domain/ship.ts", "production"),
    ],
)
def test_classify_file_role_with_custom_sizing_config(rel_path: str, expected_role: str):
    """Verifies custom generated extensions/basenames/globs and test globs extend defaults."""
    cfg = commit_sizing.SizingConfig(
        generated_basenames=frozenset({"schema.graphql"}),
        generated_extensions=frozenset({".pb.ts", ".g.dart"}),
        generated_globs=("src/generated/**", "**/*.gen.ts"),
        test_globs=("**/fixtures/**", "**/*.bench.ts"),
    )
    assert commit_sizing.classify_file_role(rel_path, config=cfg) == expected_role


def test_load_sizing_config_worktree_staged_invalid_and_cli_overrides(tmp_path: Path):
    """Verifies load_sizing_config parses JSON, prefers staged index, handles bad JSON, and honors CLI overrides."""
    repo = tmp_path / "sizing_cfg_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@crenet.games"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Dev"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)

    # 1. Missing config returns defaults
    default_cfg = commit_sizing.load_sizing_config(repo)
    assert default_cfg.max_commit_lines == 200
    assert default_cfg.generated_globs == ()

    # 2. Corrupt JSON falls back cleanly to defaults
    cfg_path = repo / ".crenet-governance.json"
    cfg_path.write_text("{not valid json", encoding="utf-8")
    assert commit_sizing.load_sizing_config(repo).max_commit_lines == 200

    # 3. Valid worktree config + type guards + extension normalization (`g.dart` -> `.g.dart`)
    cfg_path.write_text(
        json.dumps(
            {
                "sizing": {
                    "max_commit_lines": 120,
                    "max_commit_files": "bad",
                    "max_pr_lines": True,
                    "count_tests": True,
                    "count_comments": "bad",
                    "generated_basenames": ["schema.graphql", " "],
                    "generated_extensions": [".pb.ts", "g.dart", ""],
                    "generated_globs": ["src/generated/**", "**/*.gen.ts"],
                    "test_globs": "not-a-list",
                }
            }
        ),
        encoding="utf-8",
    )
    wt_cfg = commit_sizing.load_sizing_config(repo)
    assert wt_cfg.max_commit_lines == 120
    assert wt_cfg.max_commit_files == commit_sizing.DEFAULT_MAX_COMMIT_FILES
    assert wt_cfg.max_pr_lines == commit_sizing.DEFAULT_MAX_PR_LOGIC_LINES
    assert wt_cfg.count_tests is True
    assert wt_cfg.count_comments is False
    assert wt_cfg.generated_basenames == frozenset({"schema.graphql"})
    assert wt_cfg.generated_extensions == frozenset({".pb.ts", ".g.dart"})
    assert wt_cfg.generated_globs == ("src/generated/**", "**/*.gen.ts")
    assert wt_cfg.test_globs == ()

    # 4. Staged index config takes precedence over modified worktree config
    subprocess.run(["git", "add", ".crenet-governance.json"], cwd=repo, check=True)
    cfg_path.write_text(json.dumps({"sizing": {"max_commit_lines": 999}}), encoding="utf-8")
    staged_cfg = commit_sizing.load_sizing_config(
        repo,
        staged={".crenet-governance.json": "A"},
        max_commit_lines=75,
        count_comments=True,
    )
    assert staged_cfg.max_commit_lines == 75
    assert staged_cfg.count_tests is True
    assert staged_cfg.count_comments is True
