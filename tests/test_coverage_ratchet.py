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


def test_coverage_cache_bounded_eviction_preserves_pinned_upstream(git_repo: Path, tmp_path: Path):
    """Verifies CoverageCache stays bounded (<= 20 entries) while preserving pinned upstream merge-base entries."""
    cache_file = tmp_path / "bounded-cov-cache.json"
    cache = coverage_ratchet.CoverageCache(git_repo, cache_path_override=cache_file)
    base_snap = coverage_ratchet.CoverageSnapshot(
        total_lines=20, covered_lines=18, missed_lines=2, pct=90.0, files={}
    )

    cache.put(
        "origin_tree_sha",
        "fp1",
        base_snap,
        commit_sha="origin_sha",
        label="origin/main",
        pinned=True,
    )
    for idx in range(22):
        cache.put(
            f"rolling_tree_{idx}",
            "fp1",
            base_snap,
            commit_sha=f"sha_{idx}",
            label="HEAD",
            pinned=False,
        )

    assert len(cache.entries) == 1 + coverage_ratchet.MAX_ROLLING_COV_ENTRIES
    assert (
        len(cache.entries)
        <= coverage_ratchet.MAX_PINNED_COV_ENTRIES + coverage_ratchet.MAX_ROLLING_COV_ENTRIES
    )

    reloaded = coverage_ratchet.CoverageCache(git_repo, cache_path_override=cache_file)
    assert reloaded.get("origin_tree_sha", "fp1") is not None
    assert reloaded.get("rolling_tree_0", "fp1") is None
    assert reloaded.get("rolling_tree_21", "fp1") is not None


@pytest.mark.parametrize(
    ("repo_setup", "expected_stack", "expected_installed", "expected_hint_substr"),
    [
        pytest.param(
            {
                "package.json": '{"devDependencies": {"vitest": "^2.0.0"}}\n',
                "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            },
            "vitest",
            False,
            "pnpm add -D @vitest/coverage-v8",
            id="vitest_pnpm_missing_v8",
        ),
        pytest.param(
            {
                "package.json": '{"devDependencies": {"jest": "^29.0.0"}}\n',
            },
            "jest",
            True,
            None,
            id="jest_builtin_coverage",
        ),
        pytest.param(
            {
                "pyproject.toml": '[project]\nname = "demo"\n',
                "uv.lock": "version = 1\n",
            },
            "pytest",
            False,
            "uv add --dev coverage",
            id="pytest_uv_missing_coverage",
        ),
        pytest.param(
            {
                "pyproject.toml": '[project]\nname = "demo"\n',
                ".crenet-governance.json": '{"coverage": false}\n',
            },
            None,
            None,
            None,
            id="explicit_opt_out",
        ),
        pytest.param({}, "unknown", False, None, id="unknown_fallback"),
        pytest.param(
            {"Cargo.toml": '[package]\nname = "game"\n'},
            "cargo",
            True,
            None,
            id="custom_registered_detector",
        ),
    ],
)
def test_detect_coverage_onboarding_across_stacks(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_setup: dict[str, str],
    expected_stack: str | None,
    expected_installed: bool | None,
    expected_hint_substr: str | None,
):
    """Verifies self-healing onboarding detects Vitest, Jest, Pytest, custom detectors, and opt-outs."""
    for rel_path, content in repo_setup.items():
        p = git_repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    if expected_stack == "cargo":
        monkeypatch.setitem(
            coverage_ratchet.STACK_ONBOARDING_DETECTORS,
            "cargo",
            lambda r: (
                coverage_ratchet._build_onboarding_payload(
                    "cargo",
                    label="Cargo",
                    command="cargo llvm-cov --lcov --output-path lcov.info",
                    report_path="lcov.info",
                    package_installed=True,
                )
                if (r / "Cargo.toml").is_file()
                else None
            ),
        )

    onboarding = coverage_ratchet.detect_coverage_onboarding(git_repo)
    if expected_stack is None:
        assert onboarding is None
    else:
        assert onboarding is not None
        assert onboarding["detected_stack"] == expected_stack
        assert onboarding["package_installed"] is expected_installed
        if expected_hint_substr is None:
            assert onboarding["install_hint"] is None
        else:
            assert expected_hint_substr in (onboarding["install_hint"] or "")


@pytest.mark.parametrize(
    ("current_stat", "expected_regressed", "expected_delta_missed", "expected_files"),
    [
        pytest.param(
            coverage_ratchet.FileCoverageStat(12, 9, 3),
            True,
            1,
            ["src/a.py"],
            id="missed_lines_regressed",
        ),
        pytest.param(
            coverage_ratchet.FileCoverageStat(6, 4, 2),
            False,
            0,
            [],
            id="dead_code_deleted_pct_drops_not_regressed",
        ),
    ],
)
def test_compare_coverage_snapshots(
    current_stat: coverage_ratchet.FileCoverageStat,
    expected_regressed: bool,
    expected_delta_missed: int,
    expected_files: list[str],
):
    """Verifies compare_coverage_snapshots tracks missed-line deltas rather than raw percentage drops."""
    baseline = coverage_ratchet.CoverageSnapshot(
        total_lines=10,
        covered_lines=8,
        missed_lines=2,
        pct=80.0,
        files={"src/a.py": coverage_ratchet.FileCoverageStat(10, 8, 2)},
    )
    current = coverage_ratchet.CoverageSnapshot(
        total_lines=current_stat.total,
        covered_lines=current_stat.covered,
        missed_lines=current_stat.missed,
        pct=round(100.0 * current_stat.covered / current_stat.total, 2),
        files={"src/a.py": current_stat},
    )
    diff = coverage_ratchet.compare_coverage_snapshots(current, baseline, ["src/a.py"])
    assert diff["regressed"] is expected_regressed
    assert diff["delta_missed"] == expected_delta_missed
    assert [f["path"] for f in diff["regressed_files"]] == expected_files


def test_build_worktree_env_and_ephemeral_worktree(git_repo: Path):
    """Verifies _build_worktree_env prepends venv/node bins and _ephemeral_worktree cleans up."""
    (git_repo / ".venv" / "bin").mkdir(parents=True)
    (git_repo / "web" / "node_modules" / ".bin").mkdir(parents=True)
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()

    with coverage_ratchet._ephemeral_worktree(git_repo, head_sha) as (wt_dir, wt_env):
        assert wt_dir.is_dir()
        assert (wt_dir / ".venv").is_symlink()
        assert str(wt_dir / ".venv" / "bin") in wt_env["PATH"]
        assert wt_env["COVERAGE_FILE"] == str(wt_dir / ".coverage")
    assert not wt_dir.exists()


@pytest.mark.parametrize(
    ("commands", "timeout", "expected_rc", "expected_err_substr"),
    [
        pytest.param([f"{sys.executable} -c \"print('ok')\""], 10, 0, None, id="command_passes"),
        pytest.param([f"{sys.executable} -c \"import sys; sys.exit(2)\""], 10, 2, "failed (exit 2)", id="command_fails"),
        pytest.param([f"{sys.executable} -c \"import time; time.sleep(2)\""], 1, 124, "timed out", id="command_timeout"),
    ],
)
def test_execute_worktree_commands(
    tmp_path: Path, commands: list[str], timeout: int, expected_rc: int, expected_err_substr: str | None
):
    """Verifies _execute_worktree_commands captures exit codes and timeouts."""
    results, errs = coverage_ratchet._execute_worktree_commands(commands, tmp_path, dict(os.environ), timeout)
    assert results[0]["returncode"] == expected_rc
    if expected_err_substr is None:
        assert errs == []
    else:
        assert any(expected_err_substr in e for e in errs)


@pytest.mark.parametrize(("mode", "exp_errors", "exp_warnings"), [("error", ["m"], []), ("warn", [], ["m"])])
def test_append_cov_issue(mode: str, exp_errors: list[str], exp_warnings: list[str]):
    """Verifies _append_cov_issue routes messages to errors or warnings by mode."""
    errors: list[str] = []
    warnings: list[str] = []
    coverage_ratchet._append_cov_issue("m", mode, errors, warnings)
    assert errors == exp_errors
    assert warnings == exp_warnings


def test_collect_staged_worktree_coverage_and_evaluate_baseline_ratchet(git_repo: Path, tmp_path: Path):
    """Verifies staged coverage collection (cache hit/miss/failure) and baseline ratchet evaluation."""
    cache = coverage_ratchet.CoverageCache(git_repo, cache_path_override=tmp_path / "cov-cache.json")
    cfg = coverage_ratchet.CoverageConfig(
        state="configured",
        command=f"{sys.executable} -c \"import pathlib; pathlib.Path('cov.json').write_text('{{\\\"total\\\": {{\\\"lines\\\": {{\\\"total\\\": 10, \\\"covered\\\": 8}}}}, \\\"src/a.py\\\": {{\\\"lines\\\": {{\\\"total\\\": 10, \\\"covered\\\": 8}}}}}}')\"",
        report_paths=["cov.json"],
        mode="warn",
        fingerprint="fp_r",
    )
    cmd_results: list[dict] = []
    snap, status, errs, warns = coverage_ratchet._collect_staged_worktree_coverage(
        tmp_path, dict(os.environ), "tree_1", "commit_1", cfg, cache, [], cmd_results, 30
    )
    assert status == "evaluated"
    assert snap is not None and snap.missed_lines == 2
    assert errs == [] and warns == []

    # Second call hits cache without re-running command
    cached_snap, cached_status, _, _ = coverage_ratchet._collect_staged_worktree_coverage(
        tmp_path, dict(os.environ), "tree_1", "commit_1", cfg, cache, [], cmd_results, 30
    )
    assert cached_status == "cached"
    assert cached_snap is not None and len(cmd_results) == 1

    # Missing report path triggers warning in warn mode
    missing_cfg = coverage_ratchet.CoverageConfig(state="configured", command=None, report_paths=["absent.json"], mode="warn", fingerprint="fp_m")
    _, m_status, _, m_warns = coverage_ratchet._collect_staged_worktree_coverage(
        tmp_path / "empty", dict(os.environ), "tree_missing", "commit_m", missing_cfg, cache, [], [], 30
    )
    assert m_status == "report_missing"
    assert m_warns

    # Seed HEAD & origin/main cache entries so _evaluate_baseline_ratchet evaluates delta cleanly
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()
    head_tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=git_repo, text=True).strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head_sha], cwd=git_repo, check=True)
    base_snap = coverage_ratchet.CoverageSnapshot(
        total_lines=10,
        covered_lines=9,
        missed_lines=1,
        pct=90.0,
        files={"src/a.py": coverage_ratchet.FileCoverageStat(10, 9, 1)},
    )
    cache.put(head_tree, cfg.fingerprint, base_snap, commit_sha=head_sha, label="HEAD")
    vs_head, vs_up, reg_status, r_errs, r_warns = coverage_ratchet._evaluate_baseline_ratchet(
        git_repo, snap, ["src/a.py"], cfg, cache, 30
    )
    assert reg_status == "regressed"
    assert vs_head is not None and vs_head["delta_missed"] == 1
    assert vs_up is not None and vs_up["anchor_ref"] == "origin/main"
    assert r_errs == [] and any("src/a.py (+1 missed)" in w for w in r_warns)
