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


def _chunk_budget_count(chunk: dict[str, Any], file_role: str) -> int:
    if file_role == "test":
        return int(chunk["test_lines"])
    return int(chunk["logic_lines"])


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[tuple[int, int]] = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = merged[-1]
        if s <= pe + 1:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _format_line_spec(rel_path: str, ranges: list[tuple[int, int]]) -> str:
    merged = _merge_line_ranges(ranges)
    parts = [f"{s}-{e}" if s != e else str(s) for s, e in merged]
    return f"{rel_path}:{','.join(parts)}"


def _topological_dirty_units(
    chunks: list[dict[str, Any]], dirty_indices: list[int]
) -> list[list[int]]:
    """Order dirty chunk indices topologically, collapsing mutual dependency cycles."""
    by_idx = {c["index"]: c for c in chunks}
    sym_owner: dict[str, int] = {}
    for idx in dirty_indices:
        for sym in by_idx[idx]["symbols"]:
            sym_owner.setdefault(sym, idx)

    header_idx = next(
        (idx for idx in dirty_indices if by_idx[idx]["kind"] == "header"),
        None,
    )

    deps: dict[int, set[int]] = {}
    for idx in dirty_indices:
        c_deps = {
            sym_owner[sym]
            for sym in by_idx[idx]["depends_on_symbols"]
            if sym in sym_owner and sym_owner[sym] != idx
        }
        if header_idx is not None and idx != header_idx:
            c_deps.add(header_idx)
        deps[idx] = c_deps

    units: list[list[int]] = []
    rem = set(dirty_indices)
    while rem:
        ready = sorted(idx for idx in rem if not (deps[idx] & rem))
        if ready:
            for item in ready:
                units.append([item])
                rem.remove(item)
        else:
            units.append(sorted(rem))
            break
    return units


def _pack_units_into_slices(
    units: list[list[int]],
    by_idx: dict[int, dict[str, Any]],
    *,
    file_role: str,
    target_max: int,
) -> list[list[int]]:
    """Greedy bin-packer grouping dependency units into `<= target_max`-line slices."""
    slices: list[list[int]] = []
    cur_slice: list[int] = []
    cur_count = 0

    for unit in units:
        unit_count = sum(_chunk_budget_count(by_idx[i], file_role) for i in unit)
        if cur_slice and target_max > 0 and cur_count + unit_count > target_max:
            slices.append(cur_slice)
            cur_slice = []
            cur_count = 0
        cur_slice.extend(unit)
        cur_count += unit_count
        if target_max > 0 and unit_count >= target_max:
            slices.append(cur_slice)
            cur_slice = []
            cur_count = 0

    if cur_slice:
        slices.append(cur_slice)
    return slices


def _compute_suggested_line_slices(
    rel_path: str,
    chunks: list[dict[str, Any]],
    *,
    file_role: str,
    target_max: int,
) -> list[dict[str, Any]]:
    """Topologically pack dirty symbol chunks into <= `target_max` line-range slices."""
    dirty_indices = [c["index"] for c in chunks if c["status"] in ("added", "modified")]
    if not dirty_indices:
        return []

    by_idx = {c["index"]: c for c in chunks}
    units = _topological_dirty_units(chunks, dirty_indices)
    slices = _pack_units_into_slices(units, by_idx, file_role=file_role, target_max=target_max)

    budget_key = "test_lines" if file_role == "test" else "logic_lines"
    return [
        {
            "slice_index": slice_num,
            "chunk_indices": sorted(idxs),
            budget_key: sum(by_idx[i][budget_key] for i in idxs),
            "line_spec": _format_line_spec(
                rel_path, [(by_idx[i]["start_line"], by_idx[i]["end_line"]) for i in idxs]
            ),
        }
        for slice_num, idxs in enumerate(slices, start=1)
    ]


def _changed_worktree_lines(repo: Path, rel_path: str, wt_lines: list[str]) -> tuple[bool, set[int]]:
    """Return `(in_head, changed_wt_lines)` comparing working-tree lines against `HEAD`."""
    head_res = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    if head_res.returncode != 0:
        return False, set(range(1, len(wt_lines) + 1))

    head_lines = head_res.stdout.decode("utf-8", errors="replace").splitlines()
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(None, head_lines, wt_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            changed.update(range(j1 + 1, j2 + 1))
    return True, changed


def _extract_raw_chunks_for_path(
    rel_path: str, wt_text: str, wt_lines: list[str]
) -> list[dict[str, Any]]:
    """Dispatch raw top-level symbol chunk extraction by file extension."""
    ext = Path(rel_path).suffix.lower()
    if ext in PY_EXTENSIONS:
        return _extract_py_chunks(wt_text, len(wt_lines))
    if ext in TS_JS_EXTENSIONS:
        return _extract_ts_chunks(wt_lines)
    return [
        {
            "kind": "file",
            "start_line": 1,
            "end_line": max(1, len(wt_lines)),
            "symbols": [],
            "raw_used": set(),
        }
    ]


def _build_chunk_record(
    idx: int,
    raw_chunk: dict[str, Any],
    *,
    file_role: str,
    total_lines: int,
    blank_nums: set[int],
    comment_nums: set[int],
    in_head: bool,
    changed_wt_lines: set[int],
    all_top_symbols: set[str],
) -> dict[str, Any]:
    """Compute line budgets, change status, and intra-file deps for one raw chunk."""
    s, e = int(raw_chunk["start_line"]), int(raw_chunk["end_line"])
    stats = LineStats(file_role=file_role)
    code_or_test_lines: list[int] = []
    for lineno in range(s, min(e, total_lines) + 1):
        if lineno in blank_nums:
            stats.blank_lines += 1
        elif lineno in comment_nums:
            stats.comment_lines += 1
        elif file_role == "test":
            stats.test_lines += 1
            code_or_test_lines.append(lineno)
        else:
            stats.logic_lines += 1
            code_or_test_lines.append(lineno)

    if not in_head or (code_or_test_lines and all(l in changed_wt_lines for l in code_or_test_lines)):
        status = "added"
    elif any(l in changed_wt_lines for l in range(s, min(e, total_lines) + 1)):
        status = "modified"
    else:
        status = "unchanged"

    own_syms = set(raw_chunk["symbols"])
    return {
        "index": idx,
        "kind": raw_chunk["kind"],
        "start_line": s,
        "end_line": e,
        "symbols": raw_chunk["symbols"],
        "status": status,
        "depends_on_symbols": sorted((raw_chunk["raw_used"] & all_top_symbols) - own_syms),
        "logic_lines": stats.logic_lines,
        "test_lines": stats.test_lines,
        "comment_lines": stats.comment_lines,
    }


def analyze_file_chunks(
    repo: Path,
    rel_path: str,
    *,
    sizing_config: SizingConfig | None = None,
    max_chunk_lines: int | None = None,
) -> dict[str, Any]:
    """Analyze top-level symbols, line counts, intra-file deps, and suggested `--lines` slices."""
    wt_path = repo / rel_path
    if not wt_path.is_file():
        return {
            "file": rel_path,
            "file_role": "production",
            "chunk_count": 0,
            "chunks": [],
            "suggested_line_slices": [],
        }

    cfg = sizing_config or load_sizing_config(repo)
    file_role = classify_file_role(rel_path, config=cfg)
    wt_text = wt_path.read_text(encoding="utf-8")
    wt_lines = wt_text.splitlines()
    in_head, changed_wt_lines = _changed_worktree_lines(repo, rel_path, wt_lines)
    raw_chunks = _extract_raw_chunks_for_path(rel_path, wt_text, wt_lines)
    all_top_symbols = {sym for ch in raw_chunks for sym in ch["symbols"]}
    blank_nums, comment_nums = classify_source_lines(rel_path, wt_lines)

    chunks_out = [
        _build_chunk_record(
            idx,
            ch,
            file_role=file_role,
            total_lines=len(wt_lines),
            blank_nums=blank_nums,
            comment_nums=comment_nums,
            in_head=in_head,
            changed_wt_lines=changed_wt_lines,
            all_top_symbols=all_top_symbols,
        )
        for idx, ch in enumerate(raw_chunks, start=1)
    ]

    target_max = max_chunk_lines if max_chunk_lines is not None else cfg.max_commit_lines
    suggested = _compute_suggested_line_slices(
        rel_path,
        chunks_out,
        file_role=file_role,
        target_max=target_max,
    )
    return {
        "file": rel_path,
        "file_role": file_role,
        "chunk_count": len(chunks_out),
        "chunks": chunks_out,
        "suggested_line_slices": suggested,
    }


def _collect_pattern_names(target: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(target):
        if isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _collect_local_bound_names(stmts: list[ast.stmt]) -> set[str]:
    """Collect names bound directly within a function/method body (excluding nested scopes)."""
    bound: set[str] = set()
    global_or_nonlocal: set[str] = set()
    queue: list[ast.AST] = list(stmts)
    while queue:
        node = queue.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            continue
        if isinstance(node, ast.Global):
            global_or_nonlocal.update(node.names)
            continue
        if isinstance(node, ast.Nonlocal):
            global_or_nonlocal.update(node.names)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".", 1)[0]
                if name and name != "*":
                    bound.add(name)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        if isinstance(node, ast.Lambda):
            continue
        for child in ast.iter_child_nodes(node):
            queue.append(child)
    return bound - global_or_nonlocal


def _all_arg_nodes(args: ast.arguments) -> list[ast.arg]:
    """Return every parameter `ast.arg` node declared in a function or lambda signature."""
    items = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        items.append(args.vararg)
    if args.kwarg:
        items.append(args.kwarg)
    return items


def _iter_arg_defaults(args: ast.arguments) -> list[ast.expr]:
    """Return all default value expressions attached to a function or lambda signature."""
    return [*args.defaults, *(d for d in args.kw_defaults if d is not None)]


def _walk_function_or_lambda(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    missing_syms: set[str],
    shadowed: set[str],
    found: dict[str, int],
) -> None:
    """Walk decorators/annotations/defaults under outer scope, then body with parameter shadowing."""
    all_args = _all_arg_nodes(node.args)
    if not isinstance(node, ast.Lambda):
        for dec in node.decorator_list:
            _walk_scope_for_missing_refs(dec, missing_syms, shadowed, found)
        if node.returns is not None:
            _walk_scope_for_missing_refs(node.returns, missing_syms, shadowed, found)
        for arg in all_args:
            if arg.annotation is not None:
                _walk_scope_for_missing_refs(arg.annotation, missing_syms, shadowed, found)

    for dflt in _iter_arg_defaults(node.args):
        _walk_scope_for_missing_refs(dflt, missing_syms, shadowed, found)

    inner_shadowed = set(shadowed) | {a.arg for a in all_args}
    if isinstance(node, ast.Lambda):
        _walk_scope_for_missing_refs(node.body, missing_syms, inner_shadowed, found)
        return

    inner_shadowed |= _collect_local_bound_names(node.body)
    for stmt in node.body:
        _walk_scope_for_missing_refs(stmt, missing_syms, inner_shadowed, found)


def _walk_comprehension(
    node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    missing_syms: set[str],
    shadowed: set[str],
    found: dict[str, int],
) -> None:
    """Walk comprehension clauses while progressively shadowing each generator's target bindings."""
    comp_shadowed = set(shadowed)
    for gen in node.generators:
        _walk_scope_for_missing_refs(gen.iter, missing_syms, comp_shadowed, found)
        comp_shadowed |= _collect_pattern_names(gen.target)
        for cond in gen.ifs:
            _walk_scope_for_missing_refs(cond, missing_syms, comp_shadowed, found)
    if isinstance(node, ast.DictComp):
        _walk_scope_for_missing_refs(node.key, missing_syms, comp_shadowed, found)
        _walk_scope_for_missing_refs(node.value, missing_syms, comp_shadowed, found)
    else:
        _walk_scope_for_missing_refs(node.elt, missing_syms, comp_shadowed, found)


def _walk_scope_for_missing_refs(
    node: ast.AST,
    missing_syms: set[str],
    shadowed: set[str],
    found: dict[str, int],
) -> None:
    """Recursively record unshadowed loads of `missing_syms` across lexical Python scopes."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        _walk_function_or_lambda(node, missing_syms, shadowed, found)
        return

    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        _walk_comprehension(node, missing_syms, shadowed, found)
        return

    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        if node.id in missing_syms and node.id not in shadowed:
            found.setdefault(node.id, getattr(node, "lineno", 1))
        return

    for child in ast.iter_child_nodes(node):
        _walk_scope_for_missing_refs(child, missing_syms, shadowed, found)


def _py_module_defs(tree: ast.Module) -> set[str]:
    defs: set[str] = set()
    for stmt in tree.body:
        _, syms = _py_stmt_kind_and_symbols(stmt)
        defs.update(syms)
    return defs


def check_staged_intrafile_refs(
    rel_path: str, staged_content: str, wt_content: str | None
) -> list[str]:
    """Detect Python syntax errors or references to unstaged intra-file top-level symbols."""
    if Path(rel_path).suffix.lower() not in PY_EXTENSIONS:
        return []

    try:
        staged_tree = ast.parse(staged_content, filename=rel_path)
    except SyntaxError as exc:
        return [
            f"Staged file '{rel_path}' has a Python syntax error at line {exc.lineno}: {exc.msg}."
        ]

    if wt_content is None:
        return []

    try:
        wt_tree = ast.parse(wt_content, filename=rel_path)
    except SyntaxError:
        return []

    staged_defs = _py_module_defs(staged_tree)
    wt_defs = _py_module_defs(wt_tree)
    missing_top_defs = wt_defs - staged_defs
    if not missing_top_defs:
        return []

    found: dict[str, int] = {}
    for stmt in staged_tree.body:
        _walk_scope_for_missing_refs(stmt, missing_top_defs, set(), found)

    return [
        f"Staged slice of '{rel_path}' references intra-file symbol '{sym}' (line {lineno}), "
        f"which is only defined in unstaged working-tree lines of '{rel_path}'."
        for sym, lineno in sorted(found.items(), key=lambda kv: (kv[1], kv[0]))
    ]
