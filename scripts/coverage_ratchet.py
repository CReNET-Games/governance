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
