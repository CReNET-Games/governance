#!/usr/bin/env python3
"""Index-only line-range staging, intra-file symbol chunking, and static ref checks."""

from __future__ import annotations

import ast
import difflib
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from commit_sizing import (
    LineStats,
    SizingConfig,
    classify_file_role,
    classify_source_lines,
    load_sizing_config,
)

PY_EXTENSIONS = frozenset({".py", ".pyi"})
TS_JS_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"})

TS_IMPORT_LINE_RE = re.compile(r"""^\s*(?:import\b|export\s+\*|\s*['"]use (?:strict|client)['"])""")
TS_DECL_START_RE = re.compile(
    r"""^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?"""
    r"""(?:(function|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)|"""
    r"""(describe|test|it)(?:\.(?:each|skip|only))?\s*\()"""
)
IDENT_TOKEN_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\b")


def parse_line_spec(spec: str) -> tuple[str, list[tuple[int, int]]]:
    """Parse `<file_path>:<start>-<end>,<line>,...` into `(rel_path, [(start, end), ...])`."""
    if ":" not in spec:
        raise ValueError(
            f"Invalid --lines specification '{spec}'. Expected format: path/to/file:1-50,80-120"
        )
    file_part, raw_ranges = spec.rsplit(":", 1)
    if not file_part.strip() or not raw_ranges.strip():
        raise ValueError(f"Invalid --lines specification '{spec}'. Expected path and line ranges.")

    ranges: list[tuple[int, int]] = []
    for raw_tok in raw_ranges.split(","):
        tok = raw_tok.strip()
        if not tok:
            continue
        if "-" in tok:
            s_part, e_part = tok.split("-", 1)
            start = int(s_part.strip())
            end = int(e_part.strip())
        else:
            start = end = int(tok)
        if start < 1 or end < start:
            raise ValueError(
                f"Invalid line range '{tok}' in '{spec}'; expected 1 <= start <= end."
            )
        ranges.append((start, end))

    if not ranges:
        raise ValueError(f"No valid line ranges found in '{spec}'.")
    return file_part, ranges


def stage_file_lines(repo: Path, rel_path: str, ranges: list[tuple[int, int]]) -> None:
    """Stage selected 1-based working-tree line ranges into the Git index without modifying worktree."""
    wt_path = repo / rel_path
    if not wt_path.is_file():
        raise FileNotFoundError(f"Cannot stage lines for missing file '{rel_path}'.")

    wt_text = wt_path.read_text(encoding="utf-8")
    wt_lines = wt_text.splitlines(keepends=True)
    max_line = len(wt_lines)

    selected: set[int] = set()
    for start, end in ranges:
        if end > max_line:
            raise ValueError(
                f"Requested line {end} exceeds line count ({max_line}) of '{rel_path}'."
            )
        selected.update(range(start, end + 1))

    ls_res = subprocess.run(
        ["git", "ls-files", "--stage", "--", rel_path],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=True,
    )
    ls_out = ls_res.stdout.strip()

    if not ls_out:
        mode = "100755" if os.access(wt_path, os.X_OK) else "100644"
        out_lines = [wt_lines[idx] for idx in range(max_line) if (idx + 1) in selected]
    else:
        mode = ls_out.split()[0]
        show_res = subprocess.run(
            ["git", "show", f":{rel_path}"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            check=True,
        )
        index_lines = show_res.stdout.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, index_lines, wt_lines, autojunk=False)
        out_lines = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                out_lines.extend(index_lines[i1:i2])
            elif tag == "insert":
                out_lines.extend(wt_lines[j] for j in range(j1, j2) if (j + 1) in selected)
            elif tag == "replace":
                old_slice = index_lines[i1:i2]
                new_slice = wt_lines[j1:j2]
                paired = min(len(old_slice), len(new_slice))
                for k in range(paired):
                    if (j1 + k + 1) in selected:
                        out_lines.append(new_slice[k])
                    else:
                        out_lines.append(old_slice[k])
                if len(new_slice) > paired:
                    for k in range(paired, len(new_slice)):
                        if (j1 + k + 1) in selected:
                            out_lines.append(new_slice[k])
                elif len(old_slice) > paired:
                    delete_selected = (
                        not wt_lines
                        or j2 in selected
                        or (j2 + 1) in selected
                    )
                    if not delete_selected:
                        out_lines.extend(old_slice[paired:])
            elif tag == "delete":
                if not (not wt_lines or j1 in selected or (j1 + 1) in selected):
                    out_lines.extend(index_lines[i1:i2])

    if out_lines and wt_text.endswith("\n") and not out_lines[-1].endswith(("\n", "\r")):
        out_lines[-1] = out_lines[-1] + "\n"

    blob_bytes = "".join(out_lines).encode("utf-8")
    hash_res = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo),
        input=blob_bytes,
        capture_output=True,
        check=True,
    )
    blob_sha = hash_res.stdout.decode("ascii").strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"{mode},{blob_sha},{rel_path}"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
