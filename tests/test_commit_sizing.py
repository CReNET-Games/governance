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
