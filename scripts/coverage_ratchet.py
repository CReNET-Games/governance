#!/usr/bin/env python3
"""Modular coverage ratchet: stack onboarding, bounded upstream-pinned cache, and worktree verification."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Iterator
import xml.etree.ElementTree as ET

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from coverage_parsers import (  # noqa: E402
    DEFAULT_REPORT_CANDIDATES,
    MAX_FILES_PER_COV_SNAPSHOT,
    CoverageSnapshot,
    FileCoverageStat,
    collect_worktree_coverage,
    merge_coverage_snapshots,
    parse_coverage_report,
)

GOVERNANCE_CONFIG_REL_PATH = ".crenet-governance.json"
EXECUTABLE_CODE_EXTS = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".mts",
        ".cts",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".swift",
        ".kt",
    }
)
MAX_ROLLING_COV_ENTRIES = 16
MAX_PINNED_COV_ENTRIES = 4


def _git(args: list[str], repo: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=check,
    )


def _read_utf8_safe(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass
class CoverageConfig:
    state: str  # "configured" | "opted_out" | "unconfigured"
    command: str | None = None
    report_paths: list[str] = field(default_factory=list)
    mode: str = "warn"  # "warn" | "error"
    fingerprint: str = ""


def _compute_cov_fingerprint(command: str | None, report_paths: list[str]) -> str:
    payload = json.dumps({"cmd": command or "", "reports": list(report_paths)}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _read_governance_config_dict(
    repo: Path,
    staged: dict[str, str] | None = None,
    *,
    read_index_fn: Callable[[Path, str], str | None] | None = None,
) -> dict[str, Any]:
    raw_text: str | None = None
    if staged and GOVERNANCE_CONFIG_REL_PATH in staged and staged[GOVERNANCE_CONFIG_REL_PATH] != "D":
        if read_index_fn is not None:
            raw_text = read_index_fn(repo, GOVERNANCE_CONFIG_REL_PATH)
        else:
            res = subprocess.run(
                ["git", "show", f":{GOVERNANCE_CONFIG_REL_PATH}"],
                cwd=str(repo),
                capture_output=True,
                check=False,
            )
            if res.returncode == 0 and b"\x00" not in res.stdout:
                raw_text = res.stdout.decode("utf-8", errors="replace")
    if raw_text is None:
        cfg_file = repo / GOVERNANCE_CONFIG_REL_PATH
        if cfg_file.is_file():
            try:
                raw_text = cfg_file.read_text(encoding="utf-8")
            except OSError:
                raw_text = None
    if not raw_text:
        return {}
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_coverage_config(
    repo: Path,
    staged: dict[str, str] | None = None,
    *,
    cli_cmd: str | None = None,
    cli_reports: list[str] | None = None,
    cli_mode: str | None = None,
    read_index_fn: Callable[[Path, str], str | None] | None = None,
) -> CoverageConfig:
    """Resolve modular coverage settings from .crenet-governance.json and CLI/MCP overrides."""
    if cli_mode == "off":
        return CoverageConfig(state="opted_out")

    cfg_dict = _read_governance_config_dict(repo, staged, read_index_fn=read_index_fn)
    raw_cov = cfg_dict.get("coverage")

    has_cli_override = bool(cli_cmd or cli_reports)
    if not has_cli_override:
        if raw_cov is False:
            return CoverageConfig(state="opted_out")
        if isinstance(raw_cov, dict) and raw_cov.get("enabled") is False:
            return CoverageConfig(state="opted_out")

    cmd: str | None = None
    report_paths: list[str] = []
    mode = "warn"
    explicitly_enabled = False

    if isinstance(raw_cov, dict):
        if raw_cov.get("enabled") is True:
            explicitly_enabled = True
        if isinstance(raw_cov.get("command"), str) and raw_cov["command"].strip():
            cmd = raw_cov["command"].strip()
        rp = raw_cov.get("report_paths", raw_cov.get("report_path"))
        if isinstance(rp, str) and rp.strip():
            report_paths = [rp.strip()]
        elif isinstance(rp, list):
            report_paths = [str(x).strip() for x in rp if str(x).strip()]
        raw_mode = str(raw_cov.get("mode", "warn")).strip().lower()
        if raw_mode in ("warn", "error"):
            mode = raw_mode

    if cli_cmd and cli_cmd.strip():
        cmd = cli_cmd.strip()
    if cli_reports:
        report_paths = [r.strip() for r in cli_reports if r and r.strip()]
    if cli_mode in ("warn", "error"):
        mode = cli_mode

    if cmd or report_paths or explicitly_enabled:
        fp = _compute_cov_fingerprint(cmd, report_paths)
        return CoverageConfig(
            state="configured",
            command=cmd,
            report_paths=report_paths,
            mode=mode,
            fingerprint=fp,
        )

    return CoverageConfig(state="unconfigured")


def _detect_js_pkg_manager(repo: Path) -> str:
    if (repo / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (repo / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _js_runner_prefixes(repo: Path) -> tuple[str, str]:
    pm = _detect_js_pkg_manager(repo)
    install_prefix = {"pnpm": "pnpm add -D", "yarn": "yarn add -D"}.get(pm, "npm install -D")
    run_prefix = {"pnpm": "pnpm exec", "yarn": "yarn"}.get(pm, "npx")
    return install_prefix, run_prefix


def _collect_js_dependencies(repo: Path) -> set[str]:
    pkg_files = [repo / "package.json"]
    try:
        for child in sorted(repo.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name != "node_modules":
                cand = child / "package.json"
                if cand.is_file():
                    pkg_files.append(cand)
    except OSError:
        pass

    all_js_deps: set[str] = set()
    for pf in pkg_files:
        if not pf.is_file():
            continue
        try:
            pdata = json.loads(pf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(pdata, dict):
            for section in ("dependencies", "devDependencies"):
                sec = pdata.get(section)
                if isinstance(sec, dict):
                    all_js_deps.update(sec.keys())
    return all_js_deps


def _build_onboarding_payload(
    stack: str,
    *,
    label: str,
    command: str,
    report_path: str,
    package_installed: bool,
    install_hint: str | None = None,
) -> dict[str, Any]:
    hint_note = f"(after `{install_hint}`) " if install_hint else ""
    return {
        "detected_stack": stack,
        "package_installed": package_installed,
        "install_hint": install_hint,
        "suggested_config": {
            "coverage": {
                "command": command,
                "report_path": report_path,
                "mode": "warn",
            }
        },
        "opt_out_config": {"coverage": False},
        "message": (
            f"Coverage is not configured in `.crenet-governance.json` (detected {label}). "
            f"Ask the user once whether to configure `.crenet-governance.json` "
            f'{hint_note}or opt out with `{{"coverage": false}}`.'
        ),
    }


def _detect_vitest_stack(repo: Path) -> dict[str, Any] | None:
    deps = _collect_js_dependencies(repo)
    if "vitest" not in deps and not any(repo.glob("vitest.config.*")):
        return None
    install_prefix, run_prefix = _js_runner_prefixes(repo)
    has_cov_pkg = bool({"@vitest/coverage-v8", "@vitest/coverage-istanbul"} & deps)
    install_hint = None if has_cov_pkg else f"{install_prefix} @vitest/coverage-v8"
    return _build_onboarding_payload(
        "vitest",
        label="Vitest",
        command=f"{run_prefix} vitest run --coverage --coverage.reporter=json-summary",
        report_path="coverage/coverage-summary.json",
        package_installed=has_cov_pkg,
        install_hint=install_hint,
    )


def _detect_jest_stack(repo: Path) -> dict[str, Any] | None:
    deps = _collect_js_dependencies(repo)
    if "jest" not in deps and not any(repo.glob("jest.config.*")):
        return None
    _, run_prefix = _js_runner_prefixes(repo)
    return _build_onboarding_payload(
        "jest",
        label="Jest",
        command=f"{run_prefix} jest --coverage --coverageReporters=json-summary",
        report_path="coverage/coverage-summary.json",
        package_installed=True,
    )


def _detect_pytest_stack(repo: Path) -> dict[str, Any] | None:
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return None
    py_text = _read_utf8_safe(pyproject) or ""
    has_cov_pkg = "coverage" in py_text or "pytest-cov" in py_text
    has_uv = (repo / "uv.lock").is_file()
    install_hint = (
        None if has_cov_pkg else ("uv add --dev coverage" if has_uv else "pip install coverage")
    )
    return _build_onboarding_payload(
        "pytest",
        label="Python/pytest",
        command="coverage run -m pytest -q && coverage json -o coverage.json",
        report_path="coverage.json",
        package_installed=has_cov_pkg,
        install_hint=install_hint,
    )


def _unknown_stack_onboarding_payload() -> dict[str, Any]:
    return {
        "detected_stack": "unknown",
        "package_installed": False,
        "install_hint": None,
        "suggested_config": {
            "coverage": {
                "command": "<your-coverage-test-command>",
                "report_path": "coverage/coverage-summary.json",
                "mode": "warn",
            }
        },
        "opt_out_config": {"coverage": False},
        "message": (
            "Coverage is not configured in `.crenet-governance.json`. "
            "Ask the user once whether to configure a coverage command/report (`coverage-summary.json`, "
            '`lcov.info`, `coverage.json`, or `coverage.xml`) or opt out with `{"coverage": false}`.'
        ),
    }


OnboardingDetector = Callable[[Path], dict[str, Any] | None]
STACK_ONBOARDING_DETECTORS: dict[str, OnboardingDetector] = {
    "vitest": _detect_vitest_stack,
    "jest": _detect_jest_stack,
    "pytest": _detect_pytest_stack,
}


def detect_coverage_onboarding(
    repo: Path, staged: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """Detect test stack via `STACK_ONBOARDING_DETECTORS` and return a self-healing recipe."""
    if load_coverage_config(repo, staged).state != "unconfigured":
        return None

    for detector in STACK_ONBOARDING_DETECTORS.values():
        detected = detector(repo)
        if detected is not None:
            return detected

    return _unknown_stack_onboarding_payload()


class CoverageCache:
    """Bounded LRU cache for coverage snapshots with pinned remote/upstream branch anchors."""

    def __init__(self, repo: Path, *, cache_path_override: Path | None = None) -> None:
        self.repo = repo
        self._override = cache_path_override
        repo_hash = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]
        self.primary_path = cache_path_override or (repo / ".git" / "crenet-coverage-cache.json")
        self.fallback_path = (
            cache_path_override
            or Path(tempfile.gettempdir()) / f"crenet-coverage-cache-{repo_hash}.json"
        )
        self.seq = 0
        self.entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        candidates = [self.primary_path]
        if self.fallback_path != self.primary_path:
            candidates.append(self.fallback_path)
        for p in candidates:
            if not p.is_file():
                continue
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            self.seq = max(self.seq, int(raw.get("seq", 0)))
            raw_entries = raw.get("entries")
            if isinstance(raw_entries, dict):
                for k, v in raw_entries.items():
                    if isinstance(v, dict) and (
                        k not in self.entries
                        or int(v.get("seq", 0)) > int(self.entries[k].get("seq", 0))
                    ):
                        self.entries[k] = v

    def _evict(self) -> None:
        pinned_keys = [k for k, v in self.entries.items() if v.get("pinned")]
        rolling_keys = [k for k, v in self.entries.items() if not v.get("pinned")]

        if len(pinned_keys) > MAX_PINNED_COV_ENTRIES:
            pinned_keys.sort(key=lambda k: int(self.entries[k].get("seq", 0)))
            for victim in pinned_keys[: len(pinned_keys) - MAX_PINNED_COV_ENTRIES]:
                del self.entries[victim]

        if len(rolling_keys) > MAX_ROLLING_COV_ENTRIES:
            rolling_keys.sort(key=lambda k: int(self.entries[k].get("seq", 0)))
            for victim in rolling_keys[: len(rolling_keys) - MAX_ROLLING_COV_ENTRIES]:
                del self.entries[victim]

    def _save(self) -> None:
        self._evict()
        payload = json.dumps({"version": 1, "seq": self.seq, "entries": self.entries}, indent=2)
        for target in (self.primary_path, self.fallback_path):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload, encoding="utf-8")
                return
            except OSError:
                continue

    @staticmethod
    def _make_key(tree_sha: str, fingerprint: str) -> str:
        return f"{tree_sha}:{fingerprint}"

    def get(
        self, tree_sha: str, fingerprint: str, *, pin: bool = False, label: str = ""
    ) -> CoverageSnapshot | None:
        key = self._make_key(tree_sha, fingerprint)
        rec = self.entries.get(key)
        if not isinstance(rec, dict) or not isinstance(rec.get("snapshot"), dict):
            return None
        self.seq += 1
        rec["seq"] = self.seq
        if pin and not rec.get("pinned"):
            rec["pinned"] = True
        if label:
            rec["label"] = label
        self._save()
        return CoverageSnapshot.from_dict(rec["snapshot"])

    def put(
        self,
        tree_sha: str,
        fingerprint: str,
        snapshot: CoverageSnapshot,
        *,
        commit_sha: str = "",
        label: str = "",
        pinned: bool = False,
    ) -> None:
        key = self._make_key(tree_sha, fingerprint)
        prev = self.entries.get(key) or {}
        self.seq += 1
        self.entries[key] = {
            "tree_sha": tree_sha,
            "commit_sha": commit_sha or prev.get("commit_sha", ""),
            "label": label or prev.get("label", ""),
            "pinned": bool(pinned or prev.get("pinned", False)),
            "seq": self.seq,
            "snapshot": snapshot.to_dict(),
        }
        self._save()


def resolve_upstream_anchor(repo: Path) -> tuple[str, str, str] | None:
    """Resolve `(ref_label, merge_base_commit_sha, merge_base_tree_sha)` for remote tracking branch."""
    if _git(["rev-parse", "--verify", "HEAD"], repo, check=False).returncode != 0:
        return None

    candidate_refs: list[str] = []
    up_res = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], repo, check=False)
    if up_res.returncode == 0 and up_res.stdout.strip():
        candidate_refs.append(up_res.stdout.strip())
    candidate_refs.extend(["origin/main", "origin/master", "origin/HEAD"])

    seen: set[str] = set()
    for ref in candidate_refs:
        if ref in seen:
            continue
        seen.add(ref)
        verify_res = _git(["rev-parse", "--verify", ref], repo, check=False)
        if verify_res.returncode != 0:
            continue
        mb_res = _git(["merge-base", "HEAD", ref], repo, check=False)
        if mb_res.returncode != 0 or not mb_res.stdout.strip():
            continue
        base_sha = mb_res.stdout.strip()
        tree_res = _git(["rev-parse", f"{base_sha}^{{tree}}"], repo, check=False)
        if tree_res.returncode == 0 and tree_res.stdout.strip():
            return (ref, base_sha, tree_res.stdout.strip())
    return None


def compare_coverage_snapshots(
    current: CoverageSnapshot,
    baseline: CoverageSnapshot,
    staged_prod_paths: Iterable[str],
) -> dict[str, Any]:
    """Compare `current` coverage against `baseline` using uncovered (`missed_lines`) deltas."""
    delta_missed = current.missed_lines - baseline.missed_lines
    delta_pct = round(current.pct - baseline.pct, 2)

    regressed_files: list[dict[str, Any]] = []
    for p in sorted(set(staged_prod_paths)):
        cur_f = current.files.get(p)
        if cur_f is None:
            continue
        base_f = baseline.files.get(p)
        base_missed = base_f.missed if base_f is not None else 0
        f_delta = cur_f.missed - base_missed
        if f_delta > 0:
            regressed_files.append(
                {
                    "path": p,
                    "baseline_missed": base_missed,
                    "current_missed": cur_f.missed,
                    "delta_missed": f_delta,
                }
            )

    regressed = delta_missed > 0
    return {
        "baseline_missed": baseline.missed_lines,
        "current_missed": current.missed_lines,
        "delta_missed": delta_missed,
        "baseline_pct": baseline.pct,
        "current_pct": current.pct,
        "delta_pct": delta_pct,
        "regressed": regressed,
        "regressed_files": regressed_files,
    }


def _build_worktree_env(tmp_dir: Path) -> dict[str, str]:
    """Build environment for ephemeral worktrees with `.venv/bin` and `node_modules/.bin` on PATH."""
    env = dict(os.environ)
    extra_bins: list[str] = []
    for venv_bin in (tmp_dir / ".venv" / "bin", tmp_dir / ".venv" / "Scripts"):
        if venv_bin.exists():
            extra_bins.append(str(venv_bin))
    root_nm_bin = tmp_dir / "node_modules" / ".bin"
    if root_nm_bin.exists():
        extra_bins.append(str(root_nm_bin))
    try:
        for child in sorted(tmp_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                sub_bin = child / "node_modules" / ".bin"
                if sub_bin.exists():
                    extra_bins.append(str(sub_bin))
    except OSError:
        pass

    if extra_bins:
        cur_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([*extra_bins, cur_path] if cur_path else extra_bins)
    env["COVERAGE_FILE"] = str(tmp_dir / ".coverage")
    return env


@contextlib.contextmanager
def _ephemeral_worktree(repo: Path, commit_sha: str) -> Iterator[tuple[Path, dict[str, str]]]:
    """Materialize `commit_sha` in a detached `/tmp` worktree with symlinked dependency caches."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="atomic-verify-"))
    try:
        _git(["worktree", "add", "--detach", str(tmp_dir), commit_sha], repo)
        for cache_dir in ("node_modules", ".venv"):
            src_cache = repo / cache_dir
            dst_cache = tmp_dir / cache_dir
            if src_cache.exists() and not dst_cache.exists():
                os.symlink(str(src_cache), str(dst_cache))
        for child in repo.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                sub_nm = child / "node_modules"
                dst_sub = tmp_dir / child.name / "node_modules"
                if sub_nm.exists() and (tmp_dir / child.name).is_dir() and not dst_sub.exists():
                    os.symlink(str(sub_nm), str(dst_sub))
        yield (tmp_dir, _build_worktree_env(tmp_dir))
    finally:
        _git(["worktree", "remove", "--force", str(tmp_dir)], repo, check=False)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _git(["worktree", "prune"], repo, check=False)


def _capture_baseline_coverage_snapshot(
    repo: Path,
    commit_sha: str,
    tree_sha: str,
    label: str,
    cov_cfg: CoverageConfig,
    cache: CoverageCache,
    *,
    pinned: bool = False,
    timeout: int = 120,
) -> CoverageSnapshot | None:
    """Capture and cache a baseline coverage snapshot for `commit_sha` on cold start."""
    cached = cache.get(tree_sha, cov_cfg.fingerprint, pin=pinned, label=label)
    if cached is not None:
        return cached
    if not cov_cfg.command:
        return None
    try:
        with _ephemeral_worktree(repo, commit_sha) as (wt_dir, wt_env):
            proc = subprocess.run(
                cov_cfg.command,
                cwd=str(wt_dir),
                env=wt_env,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                return None
            snap = collect_worktree_coverage(wt_dir, cov_cfg.report_paths)
            if snap is not None:
                cache.put(
                    tree_sha,
                    cov_cfg.fingerprint,
                    snap,
                    commit_sha=commit_sha,
                    label=label,
                    pinned=pinned,
                )
            return snap
    except (subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError, ValueError):
        return None


def _execute_worktree_commands(
    commands: list[str],
    tmp_dir: Path,
    wt_env: dict[str, str],
    command_timeout: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run user verification commands in the isolated ephemeral worktree."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for cmd_str in commands:
        try:
            proc = subprocess.run(
                cmd_str,
                cwd=str(tmp_dir),
                env=wt_env,
                shell=True,
                text=True,
                capture_output=True,
                timeout=command_timeout,
            )
            results.append(
                {
                    "command": cmd_str,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                }
            )
            if proc.returncode != 0:
                errors.append(
                    f"Verification command failed (exit {proc.returncode}) in isolated staged worktree: {cmd_str}"
                )
                break
        except subprocess.TimeoutExpired as exc:
            results.append(
                {
                    "command": cmd_str,
                    "returncode": 124,
                    "stdout": exc.stdout.decode("utf-8", errors="replace")
                    if isinstance(exc.stdout, bytes)
                    else (exc.stdout or ""),
                    "stderr": f"Command timed out after {command_timeout}s",
                }
            )
            errors.append(
                f"Verification command timed out after {command_timeout}s in isolated staged worktree: {cmd_str}"
            )
            break
    return results, errors


def _append_cov_issue(
    msg: str, mode: str, errors: list[str], warnings: list[str]
) -> None:
    if mode == "error":
        errors.append(msg)
    else:
        warnings.append(msg)


def _collect_staged_worktree_coverage(
    tmp_dir: Path,
    wt_env: dict[str, str],
    tree_sha: str,
    commit_sha: str,
    cov_cfg: CoverageConfig,
    cov_cache: CoverageCache,
    commands: list[str],
    command_results: list[dict[str, Any]],
    command_timeout: int,
) -> tuple[CoverageSnapshot | None, str, list[str], list[str]]:
    """Check cache or run configured coverage command inside `tmp_dir`."""
    errors: list[str] = []
    warnings: list[str] = []

    cached_snap = cov_cache.get(tree_sha, cov_cfg.fingerprint, label="staged")
    if cached_snap is not None:
        return cached_snap, "cached", errors, warnings

    if cov_cfg.command and cov_cfg.command not in commands:
        try:
            cproc = subprocess.run(
                cov_cfg.command,
                cwd=str(tmp_dir),
                env=wt_env,
                shell=True,
                text=True,
                capture_output=True,
                timeout=command_timeout,
            )
            command_results.append(
                {
                    "command": cov_cfg.command,
                    "returncode": cproc.returncode,
                    "stdout": cproc.stdout,
                    "stderr": cproc.stderr,
                }
            )
            if cproc.returncode != 0:
                msg = (
                    f"Coverage command failed (exit {cproc.returncode}) in isolated staged worktree: "
                    f"{cov_cfg.command}"
                )
                _append_cov_issue(msg, cov_cfg.mode, errors, warnings)
                return None, "command_failed", errors, warnings
        except subprocess.TimeoutExpired:
            msg = (
                f"Coverage command timed out after {command_timeout}s in isolated staged worktree: "
                f"{cov_cfg.command}"
            )
            _append_cov_issue(msg, cov_cfg.mode, errors, warnings)
            return None, "command_failed", errors, warnings

    try:
        staged_snap = collect_worktree_coverage(tmp_dir, cov_cfg.report_paths)
    except (OSError, ValueError, ET.ParseError) as exc:
        _append_cov_issue(f"Failed to parse coverage report: {exc}", cov_cfg.mode, errors, warnings)
        return None, "parse_error", errors, warnings

    if staged_snap is not None:
        cov_cache.put(
            tree_sha,
            cov_cfg.fingerprint,
            staged_snap,
            commit_sha=commit_sha,
            label="staged",
        )
        return staged_snap, "evaluated", errors, warnings

    expected_desc = ", ".join(cov_cfg.report_paths or DEFAULT_REPORT_CANDIDATES)
    _append_cov_issue(
        f"Coverage report not found after running isolated worktree checks (expected: {expected_desc}).",
        cov_cfg.mode,
        errors,
        warnings,
    )
    return None, "report_missing", errors, warnings


def _evaluate_baseline_ratchet(
    repo: Path,
    staged_snap: CoverageSnapshot,
    staged_prod_paths: list[str],
    cov_cfg: CoverageConfig,
    cov_cache: CoverageCache,
    command_timeout: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None, list[str], list[str]]:
    """Compare `staged_snap` against cached/captured `HEAD` and pinned upstream merge-base."""
    errors: list[str] = []
    warnings: list[str] = []
    vs_head: dict[str, Any] | None = None
    vs_upstream: dict[str, Any] | None = None
    new_status: str | None = None

    head_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    head_tree_sha = _git(["rev-parse", "HEAD^{tree}"], repo).stdout.strip()
    anchor = resolve_upstream_anchor(repo)
    head_is_anchor = anchor is not None and anchor[2] == head_tree_sha

    head_snap = _capture_baseline_coverage_snapshot(
        repo,
        head_sha,
        head_tree_sha,
        anchor[0] if head_is_anchor and anchor is not None else "HEAD",
        cov_cfg,
        cov_cache,
        pinned=head_is_anchor,
        timeout=command_timeout,
    )
    if head_snap is not None:
        vs_head = compare_coverage_snapshots(staged_snap, head_snap, staged_prod_paths)

    if anchor is not None:
        anchor_ref, anchor_sha, anchor_tree_sha = anchor
        upstream_snap = (
            head_snap
            if head_is_anchor
            else _capture_baseline_coverage_snapshot(
                repo,
                anchor_sha,
                anchor_tree_sha,
                anchor_ref,
                cov_cfg,
                cov_cache,
                pinned=True,
                timeout=command_timeout,
            )
        )
        if upstream_snap is not None:
            vs_upstream = compare_coverage_snapshots(staged_snap, upstream_snap, staged_prod_paths)
            vs_upstream["anchor_ref"] = anchor_ref
            vs_upstream["anchor_commit"] = anchor_sha[:12]

    if vs_head is not None and vs_head["regressed"]:
        new_status = "regressed"
        file_details = ", ".join(
            f"{rf['path']} (+{rf['delta_missed']} missed)"
            for rf in vs_head["regressed_files"][:3]
        )
        up_note = ""
        if vs_upstream is not None:
            up_note = (
                f"; vs {vs_upstream['anchor_ref']}: "
                f"{vs_upstream['delta_missed']:+d} missed ({vs_upstream['current_pct']:.2f}%)"
            )
        reg_msg = (
            f"Coverage regression ({cov_cfg.mode}): uncovered lines increased vs HEAD "
            f"({vs_head['baseline_missed']} -> {vs_head['current_missed']} missed, "
            f"{vs_head['delta_missed']:+d}; coverage {vs_head['baseline_pct']:.2f}% -> "
            f"{vs_head['current_pct']:.2f}%{up_note})."
            + (f" Regressed staged files: {file_details}." if file_details else "")
            + " Stage or add matching unit tests with this commit so coverage holds."
        )
        _append_cov_issue(reg_msg, cov_cfg.mode, errors, warnings)
    elif vs_upstream is not None and vs_upstream["regressed"]:
        warnings.append(
            f"Branch coverage warning: cumulative uncovered lines remain higher than "
            f"{vs_upstream['anchor_ref']} ({vs_upstream['baseline_missed']} -> "
            f"{vs_upstream['current_missed']} missed, {vs_upstream['delta_missed']:+d}; "
            f"{vs_upstream['baseline_pct']:.2f}% -> {vs_upstream['current_pct']:.2f}%)."
        )

    return vs_head, vs_upstream, new_status, errors, warnings


def run_worktree_and_coverage_checks(
    repo: Path,
    *,
    commands: list[str],
    cov_cfg: CoverageConfig,
    onboarding: dict[str, Any] | None,
    staged_prod_paths: list[str],
    exec_code_logic_lines: int,
    exec_code_files: list[str],
    test_files: int,
    head_exists: bool,
    has_static_errors: bool,
    skip_worktree: bool,
    command_timeout: int,
    cache_path_override: Path | None = None,
) -> dict[str, Any]:
    """Execute Check 3 (isolated worktree commands, coverage ratchet, and test-pairing fallback)."""
    errors: list[str] = []
    warnings: list[str] = []
    command_results: list[dict[str, Any]] = []
    staged_snap: CoverageSnapshot | None = None
    cov_status = (
        "opted_out"
        if cov_cfg.state == "opted_out"
        else ("skipped" if skip_worktree else cov_cfg.state)
    )
    cov_cache: CoverageCache | None = None

    need_cov_worktree = cov_cfg.state == "configured" and bool(
        cov_cfg.command or cov_cfg.report_paths
    )
    run_worktree = not has_static_errors and not skip_worktree and (bool(commands) or need_cov_worktree)

    if run_worktree:
        tree_sha = _git(["write-tree"], repo).stdout.strip()
        commit_cmd = ["commit-tree", tree_sha, "-m", "atomic-commit-verify-slice"]
        if head_exists:
            commit_cmd[2:2] = ["-p", "HEAD"]
        commit_sha = _git(commit_cmd, repo).stdout.strip()

        if cov_cfg.state == "configured":
            cov_cache = CoverageCache(repo, cache_path_override=cache_path_override)
            if not commands:
                staged_snap = cov_cache.get(tree_sha, cov_cfg.fingerprint, label="staged")
                if staged_snap is not None:
                    cov_status = "cached"

        if commands or (need_cov_worktree and staged_snap is None):
            with _ephemeral_worktree(repo, commit_sha) as (tmp_dir, wt_env):
                if commands:
                    cmd_res, cmd_errs = _execute_worktree_commands(
                        commands, tmp_dir, wt_env, command_timeout
                    )
                    command_results.extend(cmd_res)
                    errors.extend(cmd_errs)

                if not errors:
                    if cov_cfg.state == "configured" and cov_cache is not None:
                        staged_snap, cov_status, c_errs, c_warns = _collect_staged_worktree_coverage(
                            tmp_dir,
                            wt_env,
                            tree_sha,
                            commit_sha,
                            cov_cfg,
                            cov_cache,
                            commands,
                            command_results,
                            command_timeout,
                        )
                        errors.extend(c_errs)
                        warnings.extend(c_warns)
                    elif cov_cfg.state == "unconfigured" and commands:
                        try:
                            staged_snap = collect_worktree_coverage(tmp_dir, [])
                            if staged_snap is not None:
                                cov_status = "evaluated"
                        except (OSError, ValueError, ET.ParseError):
                            staged_snap = None

    vs_head: dict[str, Any] | None = None
    vs_upstream: dict[str, Any] | None = None
    if (
        not has_static_errors
        and not errors
        and staged_snap is not None
        and head_exists
        and cov_cfg.state == "configured"
    ):
        if cov_cache is None:
            cov_cache = CoverageCache(repo, cache_path_override=cache_path_override)
        vs_head, vs_upstream, reg_status, r_errs, r_warns = _evaluate_baseline_ratchet(
            repo,
            staged_snap,
            staged_prod_paths,
            cov_cfg,
            cov_cache,
            command_timeout,
        )
        if reg_status is not None:
            cov_status = reg_status
        errors.extend(r_errs)
        warnings.extend(r_warns)

    if not has_static_errors and not errors and staged_snap is None and cov_cfg.state != "opted_out":
        if exec_code_logic_lines > 0 and test_files == 0:
            sample_files = ", ".join(exec_code_files[:3])
            if len(exec_code_files) > 3:
                sample_files += f", +{len(exec_code_files) - 3} more"
            warnings.append(
                f"Test pairing warning: staged slice modifies {exec_code_logic_lines} executable logic line(s) "
                f"({sample_files}) with 0 staged test files. Pair matching unit tests in this commit "
                f"(excluded from effective_lines), or configure/opt out of coverage in .crenet-governance.json."
            )
        if onboarding is not None and not skip_worktree:
            warnings.append(onboarding["message"])

    return {
        "command_results": command_results,
        "errors": errors,
        "warnings": warnings,
        "coverage": {
            "status": cov_status,
            "state": cov_cfg.state,
            "mode": cov_cfg.mode,
            "snapshot": staged_snap.to_dict(max_files=0) if staged_snap is not None else None,
            "vs_head": vs_head,
            "vs_upstream": vs_upstream,
            "onboarding": onboarding,
        },
    }
