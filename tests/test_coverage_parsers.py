"""Parameterized unit tests for scripts/coverage_parsers.py."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

import coverage_parsers  # noqa: E402


@pytest.mark.parametrize(
    ("filename", "payload_builder", "expected_total", "expected_covered", "expected_missed"),
    [
        pytest.param(
            "coverage-summary.json",
            lambda wt: (
                "{\n"
                '  "total": {"lines": {"total": 10, "covered": 8, "pct": 80}},\n'
                f'  "{wt.as_posix()}/src/app.ts": {{"lines": {{"total": 10, "covered": 8, "pct": 80}}}},\n'
                f'  "{wt.as_posix()}/node_modules/pkg/index.js": {{"lines": {{"total": 50, "covered": 0, "pct": 0}}}}\n'
                "}\n"
            ),
            10,
            8,
            2,
            id="istanbul_coverage_summary_json",
        ),
        pytest.param(
            "coverage-final.json",
            lambda wt: (
                "{\n"
                f'  "{wt.as_posix()}/src/app.ts": {{\n'
                f'    "path": "{wt.as_posix()}/src/app.ts",\n'
                '    "s": {"0": 3, "1": 1, "2": 0, "3": 5}\n'
                "  }\n"
                "}\n"
            ),
            4,
            3,
            1,
            id="istanbul_coverage_final_json",
        ),
        pytest.param(
            "coverage.json",
            lambda _wt: (
                "{\n"
                '  "meta": {"version": "7.6.0"},\n'
                '  "files": {\n'
                '    "src/app.py": {"summary": {"num_statements": 12, "covered_lines": 9, "missing_lines": 3}}\n'
                "  },\n"
                '  "totals": {"num_statements": 12, "covered_lines": 9, "missing_lines": 3}\n'
                "}\n"
            ),
            12,
            9,
            3,
            id="python_coverage_py_json",
        ),
    ],
)
def test_parse_json_coverage_formats(
    tmp_path: Path,
    filename: str,
    payload_builder,
    expected_total: int,
    expected_covered: int,
    expected_missed: int,
):
    """Verifies _parse_json_coverage handles Istanbul summary/final and coverage.py JSON."""
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    snap = coverage_parsers._parse_json_coverage(payload_builder(wt), wt / filename, wt)
    assert snap.total_lines == expected_total
    assert snap.covered_lines == expected_covered
    assert snap.missed_lines == expected_missed


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        ("", None),
        ("../outside.py", None),
        ("./src/a.py", "src/a.py"),
        ("/unrelated/abs/a.py", None),
        (".venv/lib/x.py", None),
    ],
)
def test_normalize_cov_file_path(tmp_path: Path, raw_path: str, expected: str | None):
    """Verifies coverage path normalization filters out-of-repo and ignored directories."""
    wt = tmp_path / "wt"
    wt.mkdir()
    assert coverage_parsers._normalize_cov_file_path(raw_path, wt) == expected


def test_coverage_snapshot_roundtrip_and_capping():
    """Verifies CoverageSnapshot.to_dict(max_files=...) and from_dict roundtrip."""
    single = coverage_parsers.CoverageSnapshot(
        total_lines=4,
        covered_lines=3,
        missed_lines=1,
        pct=75.0,
        files={
            "a.py": coverage_parsers.FileCoverageStat(2, 1, 1),
            "b.py": coverage_parsers.FileCoverageStat(2, 2, 0),
        },
    )
    capped_dict = single.to_dict(max_files=1)
    assert list(capped_dict["files"].keys()) == ["a.py"]
    restored = coverage_parsers.CoverageSnapshot.from_dict(capped_dict)
    assert restored.missed_lines == 1

