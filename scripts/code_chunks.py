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


def _node_span(stmt: ast.stmt) -> tuple[int, int]:
    decs = getattr(stmt, "decorator_list", ()) or ()
    start = min([stmt.lineno, *(d.lineno for d in decs)])
    end = getattr(stmt, "end_lineno", None) or stmt.lineno
    return start, end


def _py_stmt_kind_and_symbols(stmt: ast.stmt) -> tuple[str, list[str]]:
    if isinstance(stmt, ast.ClassDef):
        return "class", [stmt.name]
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "function", [stmt.name]
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        syms: list[str] = []
        for alias in stmt.names:
            name = alias.asname or alias.name.split(".", 1)[0]
            if name and name != "*":
                syms.append(name)
        return "import", syms
    if isinstance(stmt, ast.Assign):
        names = [
            n.id
            for tgt in stmt.targets
            for n in ast.walk(tgt)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        ]
        return "constant", list(dict.fromkeys(names))
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return "constant", [stmt.target.id]
    return "statement", []


def _py_top_level_used_names(stmt: ast.stmt, own_symbols: set[str]) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in own_symbols:
                used.add(node.id)
    return used


def _extract_py_chunks(wt_text: str, total_lines: int) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(wt_text)
    except SyntaxError:
        return [
            {
                "kind": "module",
                "start_line": 1,
                "end_line": max(1, total_lines),
                "symbols": [],
                "raw_used": set(),
            }
        ]

    body = tree.body
    if not body:
        return [
            {
                "kind": "module",
                "start_line": 1,
                "end_line": max(1, total_lines),
                "symbols": [],
                "raw_used": set(),
            }
        ]

    raw_chunks: list[dict[str, Any]] = []
    idx = 0
    header_end = 0
    header_syms: list[str] = []
    if isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        if isinstance(body[0].value.value, str):
            _, header_end = _node_span(body[0])
            idx = 1

    while idx < len(body) and isinstance(body[idx], (ast.Import, ast.ImportFrom)):
        _, stmt_end = _node_span(body[idx])
        _, syms = _py_stmt_kind_and_symbols(body[idx])
        header_syms.extend(syms)
        header_end = max(header_end, stmt_end)
        idx += 1

    prev_end = 0
    if header_end > 0:
        raw_chunks.append(
            {
                "kind": "header",
                "start_line": 1,
                "end_line": header_end,
                "symbols": list(dict.fromkeys(header_syms)),
                "raw_used": set(),
            }
        )
        prev_end = header_end

    while idx < len(body):
        stmt = body[idx]
        _, stmt_end = _node_span(stmt)
        kind, syms = _py_stmt_kind_and_symbols(stmt)
        used = _py_top_level_used_names(stmt, set(syms))
        start_line = prev_end + 1
        end_line = max(start_line, stmt_end)
        raw_chunks.append(
            {
                "kind": kind,
                "start_line": start_line,
                "end_line": end_line,
                "symbols": syms,
                "raw_used": used,
            }
        )
        prev_end = end_line
        idx += 1

    if raw_chunks and total_lines > prev_end:
        raw_chunks[-1]["end_line"] = total_lines

    return raw_chunks


def _extract_ts_chunks(wt_lines: list[str]) -> list[dict[str, Any]]:
    total_lines = len(wt_lines)
    if total_lines == 0:
        return []

    raw_chunks: list[dict[str, Any]] = []
    header_end = 0
    in_import_block = False
    for idx, line in enumerate(wt_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "*/")):
            continue
        if in_import_block:
            header_end = idx
            if ";" in line or " from " in line:
                in_import_block = False
            continue
        if TS_IMPORT_LINE_RE.match(line):
            header_end = idx
            if "{" in line and "}" not in line:
                in_import_block = True
            continue
        break

    prev_end = 0
    if header_end > 0:
        raw_chunks.append(
            {
                "kind": "header",
                "start_line": 1,
                "end_line": header_end,
                "symbols": [],
                "raw_used": set(),
            }
        )
        prev_end = header_end

    depth = 0
    cur_start: int | None = None
    cur_kind = "statement"
    cur_syms: list[str] = []
    cur_tokens: set[str] = set()

    for lineno in range(prev_end + 1, total_lines + 1):
        line = wt_lines[lineno - 1]
        stripped = line.strip()

        if cur_start is None and depth == 0:
            m = TS_DECL_START_RE.match(line)
            if m:
                cur_start = prev_end + 1
                cur_kind = m.group(1) or m.group(3) or "statement"
                sym = m.group(2)
                cur_syms = [sym] if sym else []
                cur_tokens = set(IDENT_TOKEN_RE.findall(line)) - set(cur_syms)
            elif stripped and not stripped.startswith(("//", "/*", "*", "*/")):
                cur_start = prev_end + 1
                cur_kind = "statement"
                cur_syms = []
                cur_tokens = set(IDENT_TOKEN_RE.findall(line))

        elif cur_start is not None:
            cur_tokens.update(IDENT_TOKEN_RE.findall(line))

        depth += line.count("{") + line.count("(") - line.count("}") - line.count(")")
        depth = max(0, depth)

        if cur_start is not None and depth == 0:
            is_multiline_header = stripped.endswith("{") or stripped.endswith("(")
            if not is_multiline_header:
                raw_chunks.append(
                    {
                        "kind": cur_kind,
                        "start_line": cur_start,
                        "end_line": lineno,
                        "symbols": cur_syms,
                        "raw_used": cur_tokens - set(cur_syms),
                    }
                )
                prev_end = lineno
                cur_start = None
                cur_syms = []
                cur_tokens = set()

    if cur_start is not None:
        raw_chunks.append(
            {
                "kind": cur_kind,
                "start_line": cur_start,
                "end_line": total_lines,
                "symbols": cur_syms,
                "raw_used": cur_tokens - set(cur_syms),
            }
        )
    elif raw_chunks and total_lines > prev_end:
        raw_chunks[-1]["end_line"] = total_lines

    return raw_chunks
