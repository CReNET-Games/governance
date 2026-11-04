#!/usr/bin/env python3
"""Deterministic helper CLI for the Atomic Commit Chain & Code Review workflow.

Provides four subcommands:
  - status : Inspect dirty/untracked files, hunk counts, static import graph, and topological commit waves.
  - hunks  : List numbered diff hunks for a file to support non-interactive partial staging.
  - stage  : Non-interactively stage full files and/or specific numbered hunks into the Git index.
  - verify : Deterministically verify that the staged slice (HEAD + index) is self-contained,
             does not depend on unstaged files/symbols, and passes tests/typechecks in an
             isolated ephemeral git worktree without disturbing the working tree.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable


# ---------------------------------------------------------------------------
# Git execution helpers
# ---------------------------------------------------------------------------


def run_git(
    args: list[str],
    repo: Path,
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command inside the target repository."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def get_repo_root(path: Path) -> Path:
    """Resolve the top-level Git repository root for path."""
    res = run_git(["rev-parse", "--show-toplevel"], path)
    return Path(res.stdout.strip()).resolve()


def has_head_commit(repo: Path) -> bool:
    """Return True if the repository has at least one commit at HEAD."""
    res = run_git(["rev-parse", "--verify", "HEAD"], repo, check=False)
    return res.returncode == 0


# ---------------------------------------------------------------------------
# Diff & Hunk parsing / non-interactive partial staging
# ---------------------------------------------------------------------------

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffHunk:
    index: int
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section_hint: str
    body_lines: list[str]

    @property
    def preview(self) -> str:
        """Compact preview of added/removed lines inside the hunk."""
        changed = [
            line
            for line in self.body_lines
            if (line.startswith("+") or line.startswith("-"))
            and not line.startswith("+++")
            and not line.startswith("---")
        ]
        snippet = "\n".join(changed[:8])
        if len(changed) > 8:
            snippet += f"\n... (+{len(changed) - 8} more changed lines)"
        return snippet


@dataclass
class FileDiff:
    file_path: str
    header_lines: list[str]
    hunks: list[DiffHunk]


def parse_single_file_diff(repo: Path, rel_path: str, *, cached: bool = False) -> FileDiff:
    """Parse `git diff` for a single file into its file header and numbered hunks."""
    cmd = ["diff"]
    if cached:
        cmd.append("--cached")
    cmd.extend(["--no-color", "--unified=3", "--", rel_path])
    res = run_git(cmd, repo)
    raw = res.stdout
    if not raw.strip():
        return FileDiff(file_path=rel_path, header_lines=[], hunks=[])

    lines = raw.splitlines(keepends=True)
    header_lines: list[str] = []
    hunks: list[DiffHunk] = []
    current_hunk_Header: str | None = None
    current_meta: tuple[int, int, int, int, str] | None = None
    current_body: list[str] = []

    for line in lines:
        stripped = line.rstrip("\r\n")
        m = HUNK_HEADER_RE.match(stripped)
        if m:
            if current_hunk_Header is not None and current_meta is not None:
                hunks.append(
                    DiffHunk(
                        index=len(hunks) + 1,
                        header=current_hunk_Header,
                        old_start=current_meta[0],
                        old_count=current_meta[1],
                        new_start=current_meta[2],
                        new_count=current_meta[3],
                        section_hint=current_meta[4].strip(),
                        body_lines=current_body,
                    )
                )
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            section_hint = m.group(5) or ""
            current_hunk_Header = line
            current_meta = (old_start, old_count, new_start, new_count, section_hint)
            current_body = []
        else:
            if current_hunk_Header is None:
                header_lines.append(line)
            else:
                current_body.append(line)

    if current_hunk_Header is not None and current_meta is not None:
        hunks.append(
            DiffHunk(
                index=len(hunks) + 1,
                header=current_hunk_Header,
                old_start=current_meta[0],
                old_count=current_meta[1],
                new_start=current_meta[2],
                new_count=current_meta[3],
                section_hint=current_meta[4].strip(),
                body_lines=current_body,
            )
        )

    return FileDiff(file_path=rel_path, header_lines=header_lines, hunks=hunks)


def stage_file_hunks(repo: Path, rel_path: str, hunk_indices: list[int]) -> None:
    """Stage specific 1-based hunk indices for `rel_path` into the Git index."""
    file_diff = parse_single_file_diff(repo, rel_path, cached=False)
    if not file_diff.hunks:
        raise ValueError(f"No unstaged diff hunks found for '{rel_path}'.")

    available = {h.index: h for h in file_diff.hunks}
    # Sort and deduplicate indices so hunks are applied in ascending file order
    ordered_indices = sorted(set(hunk_indices))
    selected: list[DiffHunk] = []
    for idx in ordered_indices:
        if idx not in available:
            valid_range = f"1..{len(file_diff.hunks)}"
            raise ValueError(
                f"Invalid hunk index {idx} for '{rel_path}' (available hunks: {valid_range})."
            )
        selected.append(available[idx])

    patch_chunks: list[str] = list(file_diff.header_lines)
    cumulative_delta = 0
    for h in selected:
        adjusted_new_start = max(0, h.old_start + cumulative_delta) if h.new_count > 0 else 0
        section_suffix = f" {h.section_hint}" if h.section_hint else ""
        rebuilt_header = (
            f"@@ -{h.old_start},{h.old_count} "
            f"+{adjusted_new_start},{h.new_count} @@{section_suffix}\n"
        )
        patch_chunks.append(rebuilt_header)
        patch_chunks.extend(h.body_lines)
        cumulative_delta += h.new_count - h.old_count

    patch_text = "".join(patch_chunks)
    if not patch_text.endswith("\n"):
        patch_text += "\n"

    run_git(["apply", "--cached", "--recount", "-"], repo, input_text=patch_text)


# ---------------------------------------------------------------------------
# Static import & export analysis (TS/JS & Python)
# ---------------------------------------------------------------------------

TS_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
PY_EXTENSIONS = (".py",)

# Matches:
#   import ... from './foo'
#   export ... from './foo'
#   import('./foo')
#   require('./foo')
TS_IMPORT_SPEC_RE = re.compile(
    r"""(?:(?:import|export)\s+(?:type\s+)?(?:[\s\S]*?\s+from\s+)?|import\s*\(\s*|require\s*\(\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Matches named imports: import { A, B as C, type D } from './foo'
TS_NAMED_IMPORT_RE = re.compile(
    r"""import\s+(?:type\s+)?\{([^}]+)\}\s+from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Matches TS/JS exports:
#   export const/let/var/function/class/interface/type/enum Foo
TS_DECL_EXPORT_RE = re.compile(
    r"""^\s*export\s+(?:declare\s+)?(?:async\s+)?(?:const|let|var|function|class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)""",
    re.MULTILINE,
)
# Matches: export { Foo, Bar as Baz }
TS_BRACED_EXPORT_RE = re.compile(
    r"""^\s*export\s+(?:type\s+)?\{([^}]+)\}""",
    re.MULTILINE,
)

# Python imports:
#   from foo.bar import a, b
#   import foo.bar
PY_FROM_IMPORT_RE = re.compile(
    r"""^[ \t]*from[ \t]+(\.*[A-Za-z0-9_.]*)[ \t]+import[ \t]+([^\n#]+)""",
    re.MULTILINE,
)
PY_DIRECT_IMPORT_RE = re.compile(
    r"""^[ \t]*import[ \t]+([A-Za-z0-9_., \t]+)""",
    re.MULTILINE,
)
PY_TOP_DEF_RE = re.compile(
    r"""^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)|^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=""",
    re.MULTILINE,
)


def _strip_comments(source: str, ext: str) -> str:
    """Lightweight comment stripping to avoid false-positive imports in comments."""
    if ext in TS_JS_EXTENSIONS:
        source = re.sub(r"/\*[\s\S]*?\*/", "", source)
        source = re.sub(r"//[^\n]*", "", source)
    elif ext in PY_EXTENSIONS:
        source = re.sub(r"#[^\n]*", "", source)
    return source


def resolve_ts_import(importer_rel: str, specifier: str, candidate_files: set[str]) -> str | None:
    """Resolve a TS/JS import specifier to a repository-relative file path."""
    importer_dir = Path(importer_rel).parent
    possible_bases: list[Path] = []

    if specifier.startswith("."):
        possible_bases.append(Path(os.path.normpath(importer_dir / specifier)))
    else:
        # Handle workspace package or path-alias imports like `@shared/foo` or `shared/foo`
        cleaned = specifier.lstrip("@")
        possible_bases.append(Path(os.path.normpath(cleaned)))
        # Also search common monorepo roots (packages/<pkg>/src/..., etc.)
        parts = cleaned.split("/")
        if len(parts) >= 2:
            pkg, rest = parts[0], "/".join(parts[1:])
            possible_bases.append(Path(os.path.normpath(f"packages/{pkg}/src/{rest}")))
            possible_bases.append(Path(os.path.normpath(f"packages/{pkg}/{rest}")))
            possible_bases.append(Path(os.path.normpath(f"src/{cleaned}")))

    for base in possible_bases:
        base_str = base.as_posix()
        if base_str.startswith("../"):
            continue

        # 1. Exact match
        if base_str in candidate_files:
            return base_str

        # 2. TypeScript ESM `.js` / `.mjs` -> `.ts` / `.tsx` mapping
        for js_ext, ts_replacements in (
            (".js", (".ts", ".tsx", ".d.ts")),
            (".mjs", (".mts", ".ts")),
            (".jsx", (".tsx",)),
        ):
            if base_str.endswith(js_ext):
                stem = base_str[: -len(js_ext)]
                for ts_ext in ts_replacements:
                    if (stem + ts_ext) in candidate_files:
                        return stem + ts_ext

        # 3. Extensionless import -> try TS/JS extensions and index files
        for ext in (*TS_JS_EXTENSIONS, ".d.ts"):
            if (base_str + ext) in candidate_files:
                return base_str + ext
            index_candidate = f"{base_str}/index{ext}"
            if index_candidate in candidate_files:
                return index_candidate

    return None


def resolve_py_import(importer_rel: str, module_spec: str, candidate_files: set[str]) -> str | None:
    """Resolve a Python module specifier to a repository-relative file path."""
    importer_dir = Path(importer_rel).parent

    if module_spec.startswith("."):
        # Count leading dots
        dots = len(module_spec) - len(module_spec.lstrip("."))
        remainder = module_spec.lstrip(".")
        base_dir = importer_dir
        for _ in range(dots - 1):
            base_dir = base_dir.parent
        rel_mod = remainder.replace(".", "/") if remainder else ""
        search_bases = [Path(os.path.normpath(base_dir / rel_mod))]
    else:
        rel_mod = module_spec.replace(".", "/")
        search_bases = [
            Path(os.path.normpath(rel_mod)),
            Path(os.path.normpath(importer_dir / rel_mod)),
            Path(os.path.normpath(f"src/{rel_mod}")),
            Path(os.path.normpath(f"scripts/{rel_mod}")),
        ]

    for base in search_bases:
        base_str = base.as_posix()
        if f"{base_str}.py" in candidate_files:
            return f"{base_str}.py"
        if f"{base_str}/__init__.py" in candidate_files:
            return f"{base_str}/__init__.py"

    return None


@dataclass
class ImportedSymbols:
    target_file: str
    symbols: set[str] = field(default_factory=set)


def extract_file_imports(
    rel_path: str, content: str, known_repo_files: set[str]
) -> dict[str, ImportedSymbols]:
    """Return resolved imports `{target_rel_path: ImportedSymbols}` for `rel_path`."""
    ext = Path(rel_path).suffix.lower()
    cleaned = _strip_comments(content, ext)
    resolved: dict[str, ImportedSymbols] = {}

    if ext in TS_JS_EXTENSIONS:
        for m in TS_IMPORT_SPEC_RE.finditer(cleaned):
            spec = m.group(1).strip()
            target = resolve_ts_import(rel_path, spec, known_repo_files)
            if target and target != rel_path:
                resolved.setdefault(target, ImportedSymbols(target_file=target))

        for m in TS_NAMED_IMPORT_RE.finditer(cleaned):
            raw_names, spec = m.group(1), m.group(2).strip()
            target = resolve_ts_import(rel_path, spec, known_repo_files)
            if target and target != rel_path:
                entry = resolved.setdefault(target, ImportedSymbols(target_file=target))
                for part in raw_names.split(","):
                    token = part.strip()
                    if not token:
                        continue
                    if token.startswith("type "):
                        token = token[len("type ") :].strip()
                    orig_name = token.split(" as ")[0].strip()
                    if orig_name and orig_name != "*":
                        entry.symbols.add(orig_name)

    elif ext in PY_EXTENSIONS:
        for m in PY_FROM_IMPORT_RE.finditer(cleaned):
            mod_spec = m.group(1).strip()
            names_part = m.group(2).strip().strip("()")
            target = resolve_py_import(rel_path, mod_spec, known_repo_files)
            for raw_item in names_part.split(","):
                item = raw_item.strip().split(" as ")[0].strip()
                if not item or item == "*":
                    continue
                # Check if `from pkg import submodule` points to `pkg/submodule.py`
                sub_spec = f"{mod_spec}.{item}" if mod_spec else item
                sub_target = resolve_py_import(rel_path, sub_spec, known_repo_files)
                if sub_target and sub_target != rel_path:
                    resolved.setdefault(sub_target, ImportedSymbols(target_file=sub_target))
                elif target and target != rel_path:
                    entry = resolved.setdefault(target, ImportedSymbols(target_file=target))
                    entry.symbols.add(item)

        for m in PY_DIRECT_IMPORT_RE.finditer(cleaned):
            for raw_mod in m.group(1).split(","):
                mod_name = raw_mod.strip().split(" as ")[0].strip()
                if not mod_name:
                    continue
                target = resolve_py_import(rel_path, mod_name, known_repo_files)
                if target and target != rel_path:
                    resolved.setdefault(target, ImportedSymbols(target_file=target))

    return resolved


def extract_exported_symbols(rel_path: str, content: str) -> set[str]:
    """Extract exported top-level symbol identifiers from a TS/JS or Python source file."""
    ext = Path(rel_path).suffix.lower()
    cleaned = _strip_comments(content, ext)
    exports: set[str] = set()

    if ext in TS_JS_EXTENSIONS:
        for m in TS_DECL_EXPORT_RE.finditer(cleaned):
            exports.add(m.group(1))
        for m in TS_BRACED_EXPORT_RE.finditer(cleaned):
            for part in m.group(1).split(","):
                token = part.strip()
                if not token:
                    continue
                if token.startswith("type "):
                    token = token[len("type ") :].strip()
                # `export { foo as bar }` exports `bar`
                pieces = [p.strip() for p in token.split(" as ") if p.strip()]
                if pieces:
                    exports.add(pieces[-1])
    elif ext in PY_EXTENSIONS:
        for m in PY_TOP_DEF_RE.finditer(cleaned):
            sym = m.group(1) or m.group(2)
            if sym:
                exports.add(sym)

    return exports


# ---------------------------------------------------------------------------
# Repository inventory & topological sorting (`status` subcommand)
# ---------------------------------------------------------------------------


@dataclass
class DirtyFileEntry:
    path: str
    index_status: str
    worktree_status: str
    category: str  # staged, unstaged, partially_staged, untracked, deleted
    unstaged_hunks: int
    staged_hunks: int
    depends_on_dirty: list[str] = field(default_factory=list)


def get_all_repo_files(repo: Path) -> set[str]:
    """Return all tracked + untracked (non-ignored) files in the repository."""
    res = run_git(["ls-files", "--cached", "--others", "--exclude-standard"], repo)
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


def get_staged_files(repo: Path) -> dict[str, str]:
    """Return `{path: status_code}` for all files currently staged in the index vs HEAD."""
    if has_head_commit(repo):
        res = run_git(["diff", "--cached", "--name-status"], repo)
    else:
        res = run_git(["status", "--porcelain=v1"], repo)
        out: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if len(line) >= 4 and line[0] not in (" ", "?"):
                out[line[3:].strip()] = line[0]
        return out

    staged: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        path = parts[-1].strip()
        staged[path] = status[0]
    return staged


def read_index_blob(repo: Path, rel_path: str) -> str | None:
    """Read the staged version of `rel_path` from the Git index (`:rel_path`)."""
    res = run_git(["show", f":{rel_path}"], repo, check=False)
    if res.returncode != 0:
        return None
    return res.stdout


def file_exists_in_head(repo: Path, rel_path: str) -> bool:
    """Return True if `rel_path` exists in `HEAD`."""
    if not has_head_commit(repo):
        return False
    res = run_git(["cat-file", "-e", f"HEAD:{rel_path}"], repo, check=False)
    return res.returncode == 0


def compute_topological_waves(
    dirty_paths: list[str], deps_map: dict[str, list[str]]
) -> tuple[list[list[str]], list[list[str]]]:
    """Group dirty files into dependency-ordered commit waves.

    If file A imports file B (`B in deps_map[A]`), B must appear in an earlier
    (or same, if mutually dependent) wave than A.
    Returns `(waves, cycles)`.
    """
    remaining = set(dirty_paths)
    waves: list[list[str]] = []
    cycles: list[list[str]] = []

    while remaining:
        ready = sorted(
            p
            for p in remaining
            if all(dep not in remaining for dep in deps_map.get(p, []) if dep != p)
        )
        if ready:
            waves.append(ready)
            remaining -= set(ready)
        else:
            # Cycle detected among `remaining`; group the cycle together in one wave
            cycle_group = sorted(remaining)
            cycles.append(cycle_group)
            waves.append(cycle_group)
            break

    return waves, cycles


def inspect_status(repo: Path) -> dict:
    """Inspect working tree + index and build a static dependency report."""
    res = run_git(["status", "--porcelain=v1", "-uall"], repo)
    known_files = get_all_repo_files(repo)

    entries: dict[str, DirtyFileEntry] = {}
    for raw_line in res.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        xy = raw_line[:2]
        idx_s, wt_s = xy[0], xy[1]
        raw_path = raw_line[3:].strip()
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1].strip()
        rel_path = raw_path.strip('"')

        if xy == "??":
            category = "untracked"
        elif wt_s == "D" or idx_s == "D":
            category = "deleted"
        elif idx_s != " " and wt_s != " ":
            category = "partially_staged"
        elif idx_s != " ":
            category = "staged"
        else:
            category = "unstaged"

        unstaged_hunks = 0
        staged_hunks = 0
        if category in ("unstaged", "partially_staged"):
            unstaged_hunks = len(parse_single_file_diff(repo, rel_path, cached=False).hunks)
        if category in ("staged", "partially_staged"):
            staged_hunks = len(parse_single_file_diff(repo, rel_path, cached=True).hunks)

        entries[rel_path] = DirtyFileEntry(
            path=rel_path,
            index_status=idx_s,
            worktree_status=wt_s,
            category=category,
            unstaged_hunks=unstaged_hunks,
            staged_hunks=staged_hunks,
        )

    dirty_set = set(entries.keys())
    deps_map: dict[str, list[str]] = {}

    for rel_path, entry in entries.items():
        full_path = repo / rel_path
        if entry.category == "deleted" or full_path.is_symlink() or not full_path.is_file():
            deps_map[rel_path] = []
            continue
        try:
            content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            deps_map[rel_path] = []
            continue

        imported = extract_file_imports(rel_path, content, known_files)
        dirty_deps = sorted(target for target in imported if target in dirty_set)
        entry.depends_on_dirty = dirty_deps
        deps_map[rel_path] = dirty_deps

    waves, cycles = compute_topological_waves(sorted(dirty_set), deps_map)

    return {
        "repo_root": str(repo),
        "total_dirty_files": len(entries),
        "files": [asdict(entries[p]) for p in sorted(entries)],
        "topological_waves": waves,
        "mutual_dependency_cycles": cycles,
    }


# ---------------------------------------------------------------------------
# Staged slice verification (`verify` subcommand)
# ---------------------------------------------------------------------------

CONFLICT_MARKER_RE = re.compile(r"^(<<<<<<< |=======$|>>>>>>> )", re.MULTILINE)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def find_ledger_content(repo: Path) -> str:
    """Read `assets_ledger.json` if present in the repository."""
    for candidate in (repo / "docs" / "assets_ledger.json", repo / "assets_ledger.json"):
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8")
            except OSError:
                pass
    return ""


def verify_staged_slice(
    repo: Path,
    commands: list[str] | None = None,
    *,
    skip_worktree: bool = False,
) -> dict:
    """Deterministically verify that the staged slice (`HEAD + index`) is valid and self-contained."""
    staged = get_staged_files(repo)
    if not staged:
        return {
            "passed": False,
            "staged_files": [],
            "errors": ["No files are currently staged in the Git index."],
            "command_results": [],
        }

    errors: list[str] = []
    known_files = get_all_repo_files(repo)
    ledger_content = find_ledger_content(repo)
    # If assets_ledger.json is itself staged, prefer its staged content!
    for staged_path in staged:
        if os.path.basename(staged_path) == "assets_ledger.json":
            staged_ledger = read_index_blob(repo, staged_path)
            if staged_ledger is not None:
                ledger_content = staged_ledger

    # Determine which files have unstaged modifications in the working tree
    unstaged_res = run_git(["diff", "--name-only"], repo)
    unstaged_modified = {
        line.strip() for line in unstaged_res.stdout.splitlines() if line.strip()
    }

    # Check 1 & 2: Hygiene + Static Index-vs-Worktree Dependency Isolation
    for rel_path, status_code in sorted(staged.items()):
        if status_code == "D" or (repo / rel_path).is_symlink():
            continue

        # Check asset ledger compliance for staged images
        if rel_path.lower().endswith(IMAGE_EXTENSIONS):
            basename = os.path.basename(rel_path)
            if basename not in ledger_content:
                errors.append(
                    f"Staged image '{rel_path}' is missing from assets_ledger.json (must be logged via log_ai_asset)."
                )
            continue

        staged_content = read_index_blob(repo, rel_path)
        if staged_content is None:
            continue

        # Check conflict markers
        if CONFLICT_MARKER_RE.search(staged_content):
            errors.append(f"Staged file '{rel_path}' contains leftover merge conflict markers.")

        # Check imports against unstaged/untracked files and partially staged exports
        imports = extract_file_imports(rel_path, staged_content, known_files)
        for target_path, imp_info in imports.items():
            target_is_staged = target_path in staged and staged[target_path] != "D"
            target_in_head = file_exists_in_head(repo, target_path)

            if not target_is_staged and not target_in_head:
                errors.append(
                    f"Staged file '{rel_path}' imports '{target_path}', which is unstaged/untracked (not in HEAD or staged index)."
                )
                continue

            # If target_path has unstaged hunks in the working tree, verify imported symbols exist in the index/HEAD version
            if imp_info.symbols and target_path in unstaged_modified:
                index_target_content = read_index_blob(repo, target_path)
                wt_target_path = repo / target_path
                try:
                    wt_target_content = wt_target_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    wt_target_content = None

                if index_target_content is not None and wt_target_content is not None:
                    staged_exports = extract_exported_symbols(target_path, index_target_content)
                    wt_exports = extract_exported_symbols(target_path, wt_target_content)
                    for sym in sorted(imp_info.symbols):
                        if sym in wt_exports and sym not in staged_exports:
                            errors.append(
                                f"Staged file '{rel_path}' imports symbol '{sym}' from '{target_path}', "
                                f"which only exists in unstaged working-tree changes of '{target_path}'."
                            )

    command_results: list[dict] = []

    # Check 3: Ephemeral `git write-tree` + detached worktree execution
    if not errors and commands and not skip_worktree:
        tree_sha = run_git(["write-tree"], repo).stdout.strip()
        commit_cmd = ["commit-tree", tree_sha, "-m", "atomic-commit-verify-slice"]
        if has_head_commit(repo):
            commit_cmd[2:2] = ["-p", "HEAD"]
        commit_sha = run_git(commit_cmd, repo).stdout.strip()

        tmp_dir = Path(tempfile.mkdtemp(prefix="atomic-verify-"))
        try:
            run_git(["worktree", "add", "--detach", str(tmp_dir), commit_sha], repo)

            # Symlink dependency caches (node_modules, .venv) from main repo into ephemeral worktree
            for cache_dir in ("node_modules", ".venv"):
                src_cache = repo / cache_dir
                dst_cache = tmp_dir / cache_dir
                if src_cache.exists() and not dst_cache.exists():
                    os.symlink(str(src_cache), str(dst_cache))

            for cmd_str in commands:
                proc = subprocess.run(
                    cmd_str,
                    cwd=str(tmp_dir),
                    shell=True,
                    text=True,
                    capture_output=True,
                )
                command_results.append(
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
        finally:
            run_git(["worktree", "remove", "--force", str(tmp_dir)], repo, check=False)
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            run_git(["worktree", "prune"], repo, check=False)

    return {
        "passed": len(errors) == 0,
        "staged_files": sorted(staged.keys()),
        "errors": errors,
        "command_results": command_results,
    }


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def _parse_hunk_spec(spec: str) -> tuple[str, list[int]]:
    """Parse `<file_path>:<1,2,3>` into `(file_path, [1, 2, 3])`."""
    if ":" not in spec:
        raise ValueError(
            f"Invalid --hunks specification '{spec}'. Expected format: path/to/file:1,2"
        )
    file_part, nums_part = spec.rsplit(":", 1)
    indices = [int(x.strip()) for x in nums_part.split(",") if x.strip()]
    if not indices:
        raise ValueError(f"No hunk numbers specified in '{spec}'.")
    return file_part, indices


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic helper for the Atomic Commit Chain & Code Review workflow."
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the target Git repository (default: current directory).",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # status
    p_status = sub.add_parser(
        "status",
        help="Inspect dirty/untracked files, hunk counts, and static dependency commit waves.",
    )
    p_status.add_argument("--json", action="store_true", help="Output JSON report.")

    # hunks
    p_hunks = sub.add_parser(
        "hunks",
        help="List numbered diff hunks for a file to prepare non-interactive partial staging.",
    )
    p_hunks.add_argument("file", help="Repository-relative path to the file.")
    p_hunks.add_argument(
        "--cached",
        action="store_true",
        help="Inspect already-staged hunks instead of unstaged working-tree hunks.",
    )
    p_hunks.add_argument("--json", action="store_true", help="Output JSON report.")

    # stage
    p_stage = sub.add_parser(
        "stage",
        help="Stage full files and/or specific numbered hunks into the Git index.",
    )
    p_stage.add_argument(
        "--reset",
        action="store_true",
        help="Unstage all currently staged changes (`git reset`) before staging this slice.",
    )
    p_stage.add_argument(
        "--files",
        nargs="*",
        default=[],
        help="Full files (modified, untracked, or deleted) to stage with `git add`.",
    )
    p_stage.add_argument(
        "--hunks",
        nargs="*",
        default=[],
        metavar="FILE:1,2",
        help="Partial file hunks to stage in the format `path/to/file:1,3`.",
    )

    # verify
    p_verify = sub.add_parser(
        "verify",
        help="Verify that the staged index has no unstaged dependencies and passes checks in an isolated worktree.",
    )
    p_verify.add_argument(
        "--cmd",
        action="append",
        default=[],
        help="Shell command to run inside the isolated staged worktree (can be passed multiple times).",
    )
    p_verify.add_argument(
        "--skip-worktree",
        action="store_true",
        help="Only run fast static index-vs-worktree checks without creating a temporary worktree.",
    )
    p_verify.add_argument("--json", action="store_true", help="Output JSON report.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = get_repo_root(Path(args.repo))

    if args.subcommand == "status":
        report = inspect_status(repo)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Repository: {report['repo_root']}")
            print(f"Total dirty/untracked files: {report['total_dirty_files']}\n")
            if not report["files"]:
                print("Working tree is clean.")
                return 0
            print("Files:")
            for f in report["files"]:
                deps = (
                    f" -> depends on: {', '.join(f['depends_on_dirty'])}"
                    if f["depends_on_dirty"]
                    else ""
                )
                print(
                    f"  [{f['category']}] {f['path']} "
                    f"(unstaged hunks: {f['unstaged_hunks']}, staged hunks: {f['staged_hunks']}){deps}"
                )
            print("\nSuggested Topological Waves (earliest commit -> latest commit):")
            for i, wave in enumerate(report["topological_waves"], start=1):
                print(f"  Wave {i}: {', '.join(wave)}")
            if report["mutual_dependency_cycles"]:
                print("\nMutual Dependency Cycles (must be staged in the same commit):")
                for cyc in report["mutual_dependency_cycles"]:
                    print(f"  Cycle: {', '.join(cyc)}")
        return 0

    if args.subcommand == "hunks":
        fdiff = parse_single_file_diff(repo, args.file, cached=args.cached)
        if args.json:
            print(
                json.dumps(
                    {
                        "file": fdiff.file_path,
                        "hunk_count": len(fdiff.hunks),
                        "hunks": [
                            {
                                "index": h.index,
                                "header": h.header.strip(),
                                "old_start": h.old_start,
                                "old_count": h.old_count,
                                "new_start": h.new_start,
                                "new_count": h.new_count,
                                "section_hint": h.section_hint,
                                "preview": h.preview,
                            }
                            for h in fdiff.hunks
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(f"File: {fdiff.file_path} ({len(fdiff.hunks)} hunks)")
            for h in fdiff.hunks:
                print(f"\n--- Hunk {h.index}: {h.header.strip()} ---")
                print(h.preview)
        return 0

    if args.subcommand == "stage":
        if args.reset:
            if has_head_commit(repo):
                run_git(["reset"], repo)
            else:
                run_git(["rm", "--cached", "-r", "--quiet", "."], repo, check=False)
        if args.files:
            run_git(["add", "--", *args.files], repo)
        for spec in args.hunks:
            file_path, indices = _parse_hunk_spec(spec)
            stage_file_hunks(repo, file_path, indices)

        staged = get_staged_files(repo)
        print(f"Staged {len(staged)} file(s): {', '.join(sorted(staged))}")
        return 0

    if args.subcommand == "verify":
        report = verify_staged_slice(
            repo,
            commands=args.cmd,
            skip_worktree=args.skip_worktree,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            if report["passed"]:
                print(
                    f"✔ Staged slice verified ({len(report['staged_files'])} file(s)): "
                    f"{', '.join(report['staged_files'])}"
                )
                for res in report["command_results"]:
                    print(f"  ✔ Command passed: {res['command']}")
            else:
                print("❌ Staged slice verification FAILED:", file=sys.stderr)
                for err in report["errors"]:
                    print(f"  - {err}", file=sys.stderr)
                for res in report["command_results"]:
                    if res["returncode"] != 0:
                        if res["stdout"]:
                            print(res["stdout"], file=sys.stderr)
                        if res["stderr"]:
                            print(res["stderr"], file=sys.stderr)
        return 0 if report["passed"] else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
