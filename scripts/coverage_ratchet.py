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

