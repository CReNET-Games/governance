#!/usr/bin/env python3
"""Modular coverage report parsers (Istanbul JSON, coverage.py JSON, LCOV, Cobertura XML)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

DEFAULT_REPORT_CANDIDATES = (
    "coverage/coverage-summary.json",
    "coverage/coverage-final.json",
    "coverage/lcov.info",
    "lcov.info",
    "coverage.json",
    "coverage/coverage.xml",
    "coverage.xml",
)
MAX_FILES_PER_COV_SNAPSHOT = 500


@dataclass
class FileCoverageStat:
    total: int
    covered: int
    missed: int


@dataclass
class CoverageSnapshot:
    total_lines: int
    covered_lines: int
    missed_lines: int
    pct: float
    files: dict[str, FileCoverageStat] = field(default_factory=dict)

    def to_dict(self, *, max_files: int = MAX_FILES_PER_COV_SNAPSHOT) -> dict[str, Any]:
        items = list(self.files.items())
        if len(items) > max_files:
            items.sort(key=lambda kv: (-kv[1].missed, -kv[1].total, kv[0]))
            items = items[:max_files]
        return {
            "total_lines": self.total_lines,
            "covered_lines": self.covered_lines,
            "missed_lines": self.missed_lines,
            "pct": self.pct,
            "files": {
                k: {"total": v.total, "covered": v.covered, "missed": v.missed}
                for k, v in sorted(items)
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CoverageSnapshot:
        files_raw = raw.get("files") or {}
        files: dict[str, FileCoverageStat] = {}
        if isinstance(files_raw, dict):
            for k, v in files_raw.items():
                if isinstance(v, dict):
                    tot = int(v.get("total", 0))
                    cov = int(v.get("covered", 0))
                    mis = int(v.get("missed", max(0, tot - cov)))
                    files[str(k)] = FileCoverageStat(total=tot, covered=cov, missed=mis)
        tot_l = int(raw.get("total_lines", 0))
        cov_l = int(raw.get("covered_lines", 0))
        mis_l = int(raw.get("missed_lines", max(0, tot_l - cov_l)))
        pct = float(raw.get("pct", round(100.0 * cov_l / tot_l, 2) if tot_l > 0 else 100.0))
        return cls(
            total_lines=tot_l,
            covered_lines=cov_l,
            missed_lines=mis_l,
            pct=pct,
            files=files,
        )


def _normalize_cov_file_path(raw_path: str, worktree_root: Path) -> str | None:
    """Normalize a file path inside a coverage report to a worktree-relative POSIX path."""
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        return None

    wt_resolved = worktree_root.resolve()
    cand = Path(cleaned)
    rel_posix: str | None = None

    if cand.is_absolute():
        for root_cand in (worktree_root, wt_resolved):
            try:
                rel_posix = cand.relative_to(root_cand).as_posix()
                break
            except ValueError:
                pass
        if rel_posix is None:
            try:
                rel_posix = cand.resolve().relative_to(wt_resolved).as_posix()
            except ValueError:
                return None
    else:
        norm = os.path.normpath(cleaned).replace("\\", "/")
        if norm.startswith("./"):
            norm = norm[2:]
        if norm in ("", ".") or norm == ".." or norm.startswith("../"):
            return None
        rel_posix = norm

    parts = rel_posix.split("/")
    if any(seg in ("node_modules", ".venv", "venv", ".git") for seg in parts):
        return None
    return "/".join(parts)


def _finalize_snapshot_from_files(
    files: dict[str, FileCoverageStat],
    fallback_total: int = 0,
    fallback_covered: int = 0,
) -> CoverageSnapshot:
    if files:
        tot = sum(v.total for v in files.values())
        cov = sum(v.covered for v in files.values())
        mis = sum(v.missed for v in files.values())
    else:
        tot = max(0, fallback_total)
        cov = max(0, min(tot, fallback_covered))
        mis = max(0, tot - cov)
    pct = round(100.0 * cov / tot, 2) if tot > 0 else 100.0
    return CoverageSnapshot(
        total_lines=tot,
        covered_lines=cov,
        missed_lines=mis,
        pct=pct,
        files=files,
    )


def _parse_json_coverage(raw_text: str, report_file: Path, worktree_root: Path) -> CoverageSnapshot:
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError(f"Unsupported JSON coverage structure in {report_file}")

    files: dict[str, FileCoverageStat] = {}

    if "files" in data and isinstance(data["files"], dict) and ("meta" in data or "totals" in data):
        for raw_p, info in data["files"].items():
            if not isinstance(info, dict):
                continue
            norm_p = _normalize_cov_file_path(str(raw_p), worktree_root)
            if not norm_p:
                continue
            summary = info.get("summary") or {}
            tot = int(summary.get("num_statements", 0))
            cov = int(summary.get("covered_lines", 0))
            mis = int(summary.get("missing_lines", max(0, tot - cov)))
            files[norm_p] = FileCoverageStat(total=tot, covered=cov, missed=mis)
        totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
        return _finalize_snapshot_from_files(
            files,
            fallback_total=int(totals.get("num_statements", 0)),
            fallback_covered=int(totals.get("covered_lines", 0)),
        )

    if "total" in data and isinstance(data["total"], dict):
        for raw_p, entry in data.items():
            if raw_p == "total" or not isinstance(entry, dict):
                continue
            norm_p = _normalize_cov_file_path(str(raw_p), worktree_root)
            if not norm_p:
                continue
            metric = (
                entry.get("lines")
                if isinstance(entry.get("lines"), dict)
                else entry.get("statements")
            )
            if not isinstance(metric, dict):
                continue
            tot = int(metric.get("total", 0))
            cov = int(metric.get("covered", 0))
            files[norm_p] = FileCoverageStat(total=tot, covered=cov, missed=max(0, tot - cov))
        tot_block = data["total"].get("lines") or data["total"].get("statements") or {}
        return _finalize_snapshot_from_files(
            files,
            fallback_total=int(tot_block.get("total", 0)) if isinstance(tot_block, dict) else 0,
            fallback_covered=int(tot_block.get("covered", 0)) if isinstance(tot_block, dict) else 0,
        )

    for raw_p, entry in data.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("s"), dict):
            continue
        target_p = str(entry.get("path") or raw_p)
        norm_p = _normalize_cov_file_path(target_p, worktree_root)
        if not norm_p:
            continue
        s_map = entry["s"]
        tot = len(s_map)
        cov = sum(1 for hits in s_map.values() if isinstance(hits, (int, float)) and hits > 0)
        files[norm_p] = FileCoverageStat(total=tot, covered=cov, missed=max(0, tot - cov))
    return _finalize_snapshot_from_files(files)


def _parse_cobertura_coverage(raw_text: str, worktree_root: Path) -> CoverageSnapshot:
    root = ET.fromstring(raw_text)
    source_prefixes: list[Path] = []
    for src_el in root.findall(".//sources/source"):
        if src_el.text and src_el.text.strip():
            source_prefixes.append(Path(src_el.text.strip()))

    line_hits: dict[str, dict[int, int]] = {}
    for cls_el in root.findall(".//class"):
        fname = cls_el.attrib.get("filename", "").strip()
        if not fname:
            continue
        norm_p = _normalize_cov_file_path(fname, worktree_root)
        if norm_p and not (worktree_root / norm_p).exists() and source_prefixes:
            for sp in source_prefixes:
                resolved_norm = _normalize_cov_file_path(str(sp / fname), worktree_root)
                if resolved_norm:
                    norm_p = resolved_norm
                    break
        if not norm_p:
            continue
        fmap = line_hits.setdefault(norm_p, {})
        for line_el in cls_el.findall(".//line"):
            try:
                lno = int(line_el.attrib["number"])
                hits = int(line_el.attrib.get("hits", "0"))
            except (KeyError, ValueError):
                continue
            fmap[lno] = max(fmap.get(lno, 0), hits)

    xml_files = {
        p: FileCoverageStat(
            total=len(lmap),
            covered=sum(1 for h in lmap.values() if h > 0),
            missed=sum(1 for h in lmap.values() if h <= 0),
        )
        for p, lmap in line_hits.items()
    }
    fb_tot = int(root.attrib.get("lines-valid", "0") or 0)
    fb_cov = int(root.attrib.get("lines-covered", "0") or 0)
    return _finalize_snapshot_from_files(xml_files, fallback_total=fb_tot, fallback_covered=fb_cov)


def _parse_lcov_coverage(raw_text: str, worktree_root: Path) -> CoverageSnapshot:
    lcov_hits: dict[str, dict[int, int]] = {}
    lcov_summary_fallback: dict[str, tuple[int, int]] = {}
    cur_file: str | None = None
    cur_lf: int | None = None
    cur_lh: int | None = None

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if line.startswith("SF:"):
            cur_file = _normalize_cov_file_path(line[3:], worktree_root)
            cur_lf = None
            cur_lh = None
        elif line.startswith("DA:") and cur_file is not None:
            parts = line[3:].split(",")
            if len(parts) >= 2:
                try:
                    lno = int(parts[0])
                    hits = int(parts[1])
                    fmap = lcov_hits.setdefault(cur_file, {})
                    fmap[lno] = max(fmap.get(lno, 0), hits)
                except ValueError:
                    pass
        elif line.startswith("LF:") and cur_file is not None:
            try:
                cur_lf = int(line[3:])
            except ValueError:
                pass
        elif line.startswith("LH:") and cur_file is not None:
            try:
                cur_lh = int(line[3:])
            except ValueError:
                pass
        elif line == "end_of_record":
            if cur_file is not None and cur_file not in lcov_hits and cur_lf is not None:
                cov = cur_lh if cur_lh is not None else 0
                lcov_summary_fallback[cur_file] = (cur_lf, cov)
            cur_file = None

    lcov_files: dict[str, FileCoverageStat] = {}
    for p, lmap in lcov_hits.items():
        lcov_files[p] = FileCoverageStat(
            total=len(lmap),
            covered=sum(1 for h in lmap.values() if h > 0),
            missed=sum(1 for h in lmap.values() if h <= 0),
        )
    for p, (tot, cov) in lcov_summary_fallback.items():
        if p not in lcov_files:
            lcov_files[p] = FileCoverageStat(total=tot, covered=cov, missed=max(0, tot - cov))

    return _finalize_snapshot_from_files(lcov_files)


def parse_coverage_report(report_file: Path, worktree_root: Path) -> CoverageSnapshot:
    """Parse Istanbul JSON (`coverage-summary.json` / `coverage-final.json`), Python `coverage.json`, LCOV, or Cobertura XML."""
    raw_text = report_file.read_text(encoding="utf-8", errors="replace")
    suffix = report_file.suffix.lower()
    stripped = raw_text.lstrip()

    if suffix == ".json" or stripped.startswith("{"):
        return _parse_json_coverage(raw_text, report_file, worktree_root)
    if suffix == ".xml" or stripped.startswith("<"):
        return _parse_cobertura_coverage(raw_text, worktree_root)
    return _parse_lcov_coverage(raw_text, worktree_root)


def merge_coverage_snapshots(snapshots: list[CoverageSnapshot]) -> CoverageSnapshot:
    """Merge multiple coverage snapshots (e.g. monorepo packages or TS + Python)."""
    if not snapshots:
        return CoverageSnapshot(total_lines=0, covered_lines=0, missed_lines=0, pct=100.0, files={})
    if len(snapshots) == 1:
        return snapshots[0]

    merged_files: dict[str, FileCoverageStat] = {}
    fallback_tot = 0
    fallback_cov = 0
    for snap in snapshots:
        if snap.files:
            for p, st in snap.files.items():
                prev = merged_files.get(p)
                if prev is None or st.covered > prev.covered or st.total > prev.total:
                    merged_files[p] = st
        else:
            fallback_tot += snap.total_lines
            fallback_cov += snap.covered_lines

    base = _finalize_snapshot_from_files(merged_files)
    tot = base.total_lines + fallback_tot
    cov = base.covered_lines + fallback_cov
    mis = max(0, tot - cov)
    pct = round(100.0 * cov / tot, 2) if tot > 0 else 100.0
    return CoverageSnapshot(
        total_lines=tot,
        covered_lines=cov,
        missed_lines=mis,
        pct=pct,
        files=merged_files,
    )


def collect_worktree_coverage(
    worktree_dir: Path, report_paths: list[str]
) -> CoverageSnapshot | None:
    """Collect and parse configured or auto-discovered coverage report(s) from `worktree_dir`."""
    if report_paths:
        parsed_list: list[CoverageSnapshot] = []
        for rel_rp in report_paths:
            cand = worktree_dir / rel_rp
            if cand.is_file():
                parsed_list.append(parse_coverage_report(cand, worktree_dir))
        if not parsed_list:
            return None
        return merge_coverage_snapshots(parsed_list)

    for default_rel in DEFAULT_REPORT_CANDIDATES:
        cand = worktree_dir / default_rel
        if cand.is_file():
            return parse_coverage_report(cand, worktree_dir)
    return None
