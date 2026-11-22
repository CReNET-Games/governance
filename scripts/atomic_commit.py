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
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from code_chunks import (  # noqa: E402
    analyze_file_chunks,
    check_staged_intrafile_refs,
    parse_line_spec,
    stage_file_lines,
)
from commit_sizing import (  # noqa: E402
    DEFAULT_MAX_COMMIT_FILES,
    DEFAULT_MAX_COMMIT_LOGIC_LINES,
    DEFAULT_MAX_PR_LOGIC_LINES,
    LineStats,
    SizingConfig,
    _read_text_safe,
    _stats_from_full_text,
    classify_file_role,
    classify_source_lines,
    compute_hunk_dict,
    compute_hunk_line_stats,
    load_sizing_config,
)
from coverage_parsers import (  # noqa: E402
    DEFAULT_REPORT_CANDIDATES,
    MAX_FILES_PER_COV_SNAPSHOT,
    CoverageSnapshot,
    FileCoverageStat,
    collect_worktree_coverage,
    merge_coverage_snapshots,
    parse_coverage_report,
)
from coverage_ratchet import (  # noqa: E402
    EXECUTABLE_CODE_EXTS,
    GOVERNANCE_CONFIG_REL_PATH,
    MAX_PINNED_COV_ENTRIES,
    MAX_ROLLING_COV_ENTRIES,
    CoverageCache,
    CoverageConfig,
    _build_worktree_env,
    _capture_baseline_coverage_snapshot,
    _ephemeral_worktree,
    compare_coverage_snapshots,
    detect_coverage_onboarding,
    load_coverage_config,
    resolve_upstream_anchor,
    run_worktree_and_coverage_checks,
)


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


def parse_single_file_diff(
    repo: Path,
    rel_path: str,
    *,
    cached: bool = False,
    base_ref: str | None = None,
) -> FileDiff:
    """Parse `git diff` for a single file into its file header and numbered hunks."""
    cmd = ["diff"]
    if base_ref is not None:
        cmd.append(base_ref)
    elif cached:
        cmd.append("--cached")
    cmd.extend(["--no-color", "--unified=3", "--", rel_path])
    raw_bytes = _run_git_bytes(cmd, repo)
    raw = raw_bytes.decode("utf-8", errors="replace")
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
# Commit & PR line counting and sizing classification
# ---------------------------------------------------------------------------


def _read_git_blob_text_safe(repo: Path, spec: str) -> str | None:
    """Read a UTF-8 git object (`HEAD:path` or `:path`) safely, returning None on missing/binary."""
    res = subprocess.run(
        ["git", "show", spec],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    if res.returncode != 0 or b"\x00" in res.stdout:
        return None
    try:
        return res.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _get_classified_blobs_for_diff(
    repo: Path,
    rel_path: str,
    role: str,
    *,
    cached: bool = False,
    base_ref: str | None = None,
    new_text_override: str | None = None,
) -> tuple[tuple[set[int], set[int]] | None, tuple[set[int], set[int]] | None]:
    """Return `(old_classified, new_classified)` line sets for a diffed file."""
    if role == "generated":
        return (None, None)

    old_classified: tuple[set[int], set[int]] | None = None
    new_classified: tuple[set[int], set[int]] | None = None

    if has_head_commit(repo) and (base_ref == "HEAD" or cached):
        old_text = _read_git_blob_text_safe(repo, f"HEAD:{rel_path}")
    elif not cached and base_ref is None:
        old_text = _read_git_blob_text_safe(repo, f":{rel_path}")
    else:
        old_text = None
    if old_text is not None:
        old_classified = classify_source_lines(rel_path, old_text.splitlines())

    new_text = new_text_override
    if new_text is None:
        if cached:
            new_text = _read_git_blob_text_safe(repo, f":{rel_path}")
        else:
            new_text = _read_text_safe(repo / rel_path)
    if new_text is not None:
        new_classified = classify_source_lines(rel_path, new_text.splitlines())

    return (old_classified, new_classified)


def get_file_hunks_with_stats(
    repo: Path,
    rel_path: str,
    *,
    cached: bool = False,
    sizing_config: SizingConfig | None = None,
) -> tuple[FileDiff, list[dict]]:
    """Parse diff hunks for `rel_path` and attach full-blob categorized line stats to each hunk."""
    fdiff = parse_single_file_diff(repo, rel_path, cached=cached)
    if not fdiff.hunks:
        return (fdiff, [])

    cfg = sizing_config or load_sizing_config(repo, get_staged_files(repo) if cached else None)
    role = classify_file_role(rel_path, config=cfg)
    old_classified, new_classified = _get_classified_blobs_for_diff(
        repo, rel_path, role, cached=cached
    )
    hunk_dicts = [
        compute_hunk_dict(
            rel_path,
            h,
            old_classified=old_classified,
            new_classified=new_classified,
            config=cfg,
        )
        for h in fdiff.hunks
    ]
    return (fdiff, hunk_dicts)


def _compute_file_diff_stats(
    repo: Path,
    rel_path: str,
    *,
    cached: bool = False,
    base_ref: str | None = None,
    new_text_override: str | None = None,
    sizing_config: SizingConfig | None = None,
) -> LineStats:
    """Compute categorized LineStats for a single diffed file using full-blob context."""
    role = classify_file_role(rel_path, config=sizing_config)
    stats = LineStats(file_role=role)
    fdiff = parse_single_file_diff(repo, rel_path, cached=cached, base_ref=base_ref)
    if not fdiff.hunks:
        return stats

    old_classified, new_classified = _get_classified_blobs_for_diff(
        repo,
        rel_path,
        role,
        cached=cached,
        base_ref=base_ref,
        new_text_override=new_text_override,
    )

    for hunk in fdiff.hunks:
        hunk_stats = compute_hunk_line_stats(
            rel_path,
            hunk,
            old_classified=old_classified,
            new_classified=new_classified,
            config=sizing_config,
        )
        stats.add_other(hunk_stats)

    return stats


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
    r"""^[ \t]*from[ \t]+(\.*[A-Za-z0-9_.]*)[ \t]+import[ \t]+(?:\(([^)]*)\)|([^\n#]+))""",
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


# ---------------------------------------------------------------------------
# TS/JS & Python static import resolution
# ---------------------------------------------------------------------------

TS_JSONC_COMMENT_RE = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/")
TS_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _parse_jsonc(raw: str) -> dict:
    """Parse JSON with line/block comments and trailing commas (tsconfig.json)."""
    no_comments = TS_JSONC_COMMENT_RE.sub("", raw)
    cleaned = TS_TRAILING_COMMA_RE.sub(r"\1", no_comments)
    data = json.loads(cleaned)
    return data if isinstance(data, dict) else {}


def _load_tsconfig_merged(config_path: Path, seen: set[Path] | None = None) -> dict:
    """Load a tsconfig.json and recursively resolve relative `extends` chains."""
    seen = seen or set()
    resolved = config_path.resolve()
    if resolved in seen or not resolved.is_file():
        return {}
    seen.add(resolved)
    try:
        data = _parse_jsonc(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    merged_opts: dict = {"_base_dir": resolved.parent}
    extends_val = data.get("extends")
    if isinstance(extends_val, str) and extends_val.startswith("."):
        parent_cfg = resolved.parent / extends_val
        if parent_cfg.suffix != ".json" and (parent_cfg.with_suffix(".json")).is_file():
            parent_cfg = parent_cfg.with_suffix(".json")
        parent_opts = _load_tsconfig_merged(parent_cfg, seen)
        merged_opts.update(parent_opts)
        if "_base_dir" not in parent_opts:
            merged_opts["_base_dir"] = resolved.parent

    cur_opts = data.get("compilerOptions")
    if isinstance(cur_opts, dict):
        parent_paths = dict(merged_opts.get("paths") or {})
        cur_paths = cur_opts.get("paths")
        merged_opts.update(cur_opts)
        if isinstance(cur_opts.get("baseUrl"), str) and cur_opts["baseUrl"].strip():
            merged_opts["_base_dir"] = (resolved.parent / cur_opts["baseUrl"]).resolve()
        if isinstance(cur_paths, dict):
            parent_paths.update(cur_paths)
            merged_opts["paths"] = parent_paths
    return merged_opts


def resolve_tsconfig_aliases(repo: Path, importer_rel: str, specifier: str) -> list[Path]:
    """Expand `compilerOptions.paths` / `baseUrl` from nearest/root tsconfig.json."""
    importer_abs_dir = (repo / importer_rel).parent
    search_dirs: list[Path] = []
    cur = importer_abs_dir
    repo_res = repo.resolve()
    while True:
        search_dirs.append(cur)
        if cur.resolve() == repo_res or cur == cur.parent:
            break
        cur = cur.parent

    candidates: list[Path] = []
    seen_configs: set[Path] = set()
    for d in search_dirs:
        cfg_path = d / "tsconfig.json"
        if not cfg_path.is_file() or cfg_path.resolve() in seen_configs:
            continue
        seen_configs.add(cfg_path.resolve())
        opts = _load_tsconfig_merged(cfg_path)
        base_dir = opts.get("_base_dir") or cfg_path.parent

        paths_map = opts.get("paths")
        if isinstance(paths_map, dict):
            for pattern, targets in paths_map.items():
                if not isinstance(targets, list):
                    continue
                if "*" in pattern:
                    prefix, suffix = pattern.split("*", 1)
                    if specifier.startswith(prefix) and specifier.endswith(suffix):
                        captured = specifier[len(prefix) : len(specifier) - len(suffix) if suffix else None]
                        for tgt in targets:
                            if not isinstance(tgt, str):
                                continue
                            mapped = tgt.replace("*", captured)
                            abs_tgt = (base_dir / mapped).resolve()
                            try:
                                candidates.append(abs_tgt.relative_to(repo_res))
                            except ValueError:
                                pass
                elif specifier == pattern:
                    for tgt in targets:
                        if not isinstance(tgt, str):
                            continue
                        abs_tgt = (base_dir / tgt).resolve()
                        try:
                            candidates.append(abs_tgt.relative_to(repo_res))
                        except ValueError:
                            pass
    return candidates


def resolve_ts_import(
    importer_rel: str,
    specifier: str,
    candidate_files: set[str],
    repo: Path | None = None,
) -> str | None:
    """Resolve a TS/JS import specifier to a repository-relative file path."""
    importer_dir = Path(importer_rel).parent
    possible_bases: list[Path] = []

    if specifier.startswith("."):
        possible_bases.append(Path(os.path.normpath(importer_dir / specifier)))
    else:
        if repo is not None:
            possible_bases.extend(resolve_tsconfig_aliases(repo, importer_rel, specifier))
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
    rel_path: str,
    content: str,
    known_repo_files: set[str],
    repo: Path | None = None,
) -> dict[str, ImportedSymbols]:
    """Return resolved imports `{target_rel_path: ImportedSymbols}` for `rel_path`."""
    ext = Path(rel_path).suffix.lower()
    cleaned = _strip_comments(content, ext)
    resolved: dict[str, ImportedSymbols] = {}

    if ext in TS_JS_EXTENSIONS:
        for m in TS_IMPORT_SPEC_RE.finditer(cleaned):
            spec = m.group(1).strip()
            target = resolve_ts_import(rel_path, spec, known_repo_files, repo=repo)
            if target and target != rel_path:
                resolved.setdefault(target, ImportedSymbols(target_file=target))

        for m in TS_NAMED_IMPORT_RE.finditer(cleaned):
            raw_names, spec = m.group(1), m.group(2).strip()
            target = resolve_ts_import(rel_path, spec, known_repo_files, repo=repo)
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
            names_part = (m.group(2) if m.group(2) is not None else m.group(3)).strip().strip("()")
            target = resolve_py_import(rel_path, mod_spec, known_repo_files)
            for raw_item in names_part.split(","):
                item = raw_item.strip().split(" as ")[0].strip()
                if not item:
                    continue
                if item == "*":
                    if target and target != rel_path:
                        resolved.setdefault(target, ImportedSymbols(target_file=target))
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
    file_role: str = "production"
    raw_additions: int = 0
    raw_deletions: int = 0
    logic_lines: int = 0
    test_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    generated_lines: int = 0
    effective_lines: int = 0


def _run_git_bytes(args: list[str], repo: Path) -> bytes:
    """Run git expecting binary NUL-delimited stdout."""
    res = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    return res.stdout


def get_all_repo_files(repo: Path) -> set[str]:
    """Return all tracked + untracked (non-ignored) files in the repository."""
    raw = _run_git_bytes(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], repo)
    return {
        token.decode("utf-8", errors="surrogateescape")
        for token in raw.split(b"\x00")
        if token
    }


def get_staged_files(repo: Path) -> dict[str, str]:
    """Return `{path: status_code}` for all files currently staged in the index vs HEAD."""
    if has_head_commit(repo):
        raw = _run_git_bytes(["diff", "--cached", "--name-status", "-z"], repo)
        tokens = [t.decode("utf-8", errors="surrogateescape") for t in raw.split(b"\x00")]
        staged: dict[str, str] = {}
        i = 0
        while i < len(tokens):
            status = tokens[i]
            if not status:
                i += 1
                continue
            code = status[0]
            if code in ("R", "C") and i + 2 < len(tokens):
                staged[tokens[i + 2]] = code
                i += 3
            elif i + 1 < len(tokens):
                staged[tokens[i + 1]] = code
                i += 2
            else:
                i += 1
        return staged

    raw = _run_git_bytes(["status", "--porcelain=v1", "-z"], repo)
    tokens = [t.decode("utf-8", errors="surrogateescape") for t in raw.split(b"\x00")]
    out: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        if len(entry) >= 4:
            idx_s = entry[0]
            path = entry[3:]
            if idx_s not in (" ", "?"):
                out[path] = idx_s
            if idx_s in ("R", "C") and i + 1 < len(tokens):
                i += 1
        i += 1
    return out


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


def inspect_status(
    repo: Path,
    *,
    count_tests: bool | None = None,
    count_comments: bool | None = None,
    max_commit_lines: int | None = None,
    max_pr_lines: int | None = None,
) -> dict:
    """Inspect working tree + index, static dependencies, and categorized line budgets."""
    sizing_cfg = load_sizing_config(
        repo,
        count_tests=count_tests,
        count_comments=count_comments,
        max_commit_lines=max_commit_lines,
        max_pr_lines=max_pr_lines,
    )
    eff_count_tests = sizing_cfg.count_tests
    eff_count_comments = sizing_cfg.count_comments
    eff_max_commit_lines = sizing_cfg.max_commit_lines
    eff_max_pr_lines = sizing_cfg.max_pr_lines

    raw = _run_git_bytes(["status", "--porcelain=v1", "-z", "-uall"], repo)
    known_files = get_all_repo_files(repo)
    head_exists = has_head_commit(repo)

    entries: dict[str, DirtyFileEntry] = {}
    tokens = [t.decode("utf-8", errors="surrogateescape") for t in raw.split(b"\x00")]
    i = 0
    while i < len(tokens):
        raw_line = tokens[i]
        i += 1
        if len(raw_line) < 4:
            continue
        xy = raw_line[:2]
        idx_s, wt_s = xy[0], xy[1]
        rel_path = raw_line[3:]
        if idx_s in ("R", "C") or wt_s in ("R", "C"):
            # Consume the original source path emitted immediately after the new path under `-z`
            if i < len(tokens):
                i += 1

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

        role = classify_file_role(rel_path, config=sizing_cfg)
        full_path = repo / rel_path
        if category == "untracked":
            fstats = _stats_from_full_text(
                rel_path, _read_text_safe(full_path), config=sizing_cfg
            )
        elif head_exists:
            fstats = _compute_file_diff_stats(
                repo, rel_path, base_ref="HEAD", sizing_config=sizing_cfg
            )
        elif category == "staged":
            fstats = _stats_from_full_text(
                rel_path,
                _read_git_blob_text_safe(repo, f":{rel_path}"),
                config=sizing_cfg,
            )
        elif category in ("unstaged", "partially_staged"):
            fstats = _stats_from_full_text(
                rel_path, _read_text_safe(full_path), config=sizing_cfg
            )
        else:
            fstats = LineStats(file_role=role)

        eff_lines = fstats.effective_lines(
            count_tests=eff_count_tests, count_comments=eff_count_comments
        )

        entries[rel_path] = DirtyFileEntry(
            path=rel_path,
            index_status=idx_s,
            worktree_status=wt_s,
            category=category,
            unstaged_hunks=unstaged_hunks,
            staged_hunks=staged_hunks,
            file_role=fstats.file_role,
            raw_additions=fstats.raw_additions,
            raw_deletions=fstats.raw_deletions,
            logic_lines=fstats.logic_lines,
            test_lines=fstats.test_lines,
            comment_lines=fstats.comment_lines,
            blank_lines=fstats.blank_lines,
            generated_lines=fstats.generated_lines,
            effective_lines=eff_lines,
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

        imported = extract_file_imports(rel_path, content, known_files, repo=repo)
        dirty_deps = sorted(target for target in imported if target in dirty_set)
        entry.depends_on_dirty = dirty_deps
        deps_map[rel_path] = dirty_deps

    waves, cycles = compute_topological_waves(sorted(dirty_set), deps_map)

    total_effective = sum(e.effective_lines for e in entries.values())
    total_logic = sum(e.logic_lines for e in entries.values())
    total_test = sum(e.test_lines for e in entries.values())
    total_comment = sum(e.comment_lines for e in entries.values())
    total_blank = sum(e.blank_lines for e in entries.values())
    total_generated = sum(e.generated_lines for e in entries.values())
    total_raw_add = sum(e.raw_additions for e in entries.values())
    total_raw_del = sum(e.raw_deletions for e in entries.values())

    if not entries:
        recommended_min_commits = 0
        recommended_pr_count = 0
    else:
        recommended_min_commits = (
            max(1, math.ceil(total_effective / eff_max_commit_lines))
            if eff_max_commit_lines > 0
            else 1
        )
        recommended_pr_count = (
            max(1, math.ceil(total_effective / eff_max_pr_lines))
            if eff_max_pr_lines > 0
            else 1
        )

    size_warnings: list[str] = []
    if eff_max_pr_lines > 0 and total_effective > eff_max_pr_lines:
        size_warnings.append(
            f"PR size warning: dirty tree has {total_effective} effective lines "
            f"(target <= {eff_max_pr_lines}; tests={'included' if eff_count_tests else 'excluded'}, "
            f"comments={'included' if eff_count_comments else 'excluded'}). "
            f"Recommend splitting into ~{recommended_pr_count} stacked PRs."
        )
    for rel_path in sorted(entries):
        entry = entries[rel_path]
        if eff_max_commit_lines > 0 and entry.effective_lines > eff_max_commit_lines:
            total_hunks = entry.unstaged_hunks + entry.staged_hunks
            if total_hunks > 1:
                size_warnings.append(
                    f"File '{rel_path}' has {entry.effective_lines} effective lines across {total_hunks} hunks "
                    f"(> {eff_max_commit_lines} commit target); inspect `hunks` or `chunks` to split across commits via `--hunks` or `--lines`."
                )
            else:
                size_warnings.append(
                    f"File '{rel_path}' has {entry.effective_lines} effective lines "
                    f"(> {eff_max_commit_lines} commit target); inspect `hunks {rel_path}` (or `chunks {rel_path}`) "
                    f"and split top-level symbols + paired tests bottom-up via `--lines` unless mutually dependent."
                )

    return {
        "repo_root": str(repo),
        "total_dirty_files": len(entries),
        "files": [asdict(entries[p]) for p in sorted(entries)],
        "topological_waves": waves,
        "mutual_dependency_cycles": cycles,
        "size_summary": {
            "effective_lines": total_effective,
            "logic_lines": total_logic,
            "test_lines": total_test,
            "comment_lines": total_comment,
            "blank_lines": total_blank,
            "generated_lines": total_generated,
            "raw_additions": total_raw_add,
            "raw_deletions": total_raw_del,
            "count_tests": eff_count_tests,
            "count_comments": eff_count_comments,
            "max_commit_lines": eff_max_commit_lines,
            "max_pr_lines": eff_max_pr_lines,
            "recommended_min_commits": recommended_min_commits,
            "recommended_pr_count": recommended_pr_count,
            "warnings": size_warnings,
        },
        "coverage_onboarding": detect_coverage_onboarding(repo),
    }


# ---------------------------------------------------------------------------
# Staged slice verification (`verify` subcommand)
# ---------------------------------------------------------------------------

CONFLICT_MARKER_RE = re.compile(r"^(<<<<<<< |=======$|>>>>>>> )", re.MULTILINE)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def _load_effective_ledger_for_verify(repo: Path, staged: dict[str, str]):
    """Load validated v2 ledger data, preferring staged docs/assets_ledger.json when staged."""
    import ledger as asset_ledger

    if not asset_ledger.is_asset_provenance_enabled(repo):
        return None
    if asset_ledger.LEDGER_REL_PATH in staged and staged[asset_ledger.LEDGER_REL_PATH] != "D":
        staged_raw = read_index_blob(repo, asset_ledger.LEDGER_REL_PATH)
        if staged_raw is not None:
            data = json.loads(staged_raw)
            asset_ledger.validate_ledger_data(data)
            return data
    return asset_ledger.load_ledger(repo)


def _build_size_metrics_dict(
    stats: LineStats,
    *,
    effective_lines: int,
    production_files: int,
    test_files: int,
    generated_files: int,
    count_tests: bool,
    count_comments: bool,
    max_commit_lines: int,
    max_commit_files: int,
) -> dict:
    return {
        "effective_lines": effective_lines,
        "logic_lines": stats.logic_lines,
        "test_lines": stats.test_lines,
        "comment_lines": stats.comment_lines,
        "blank_lines": stats.blank_lines,
        "generated_lines": stats.generated_lines,
        "raw_additions": stats.raw_additions,
        "raw_deletions": stats.raw_deletions,
        "production_files": production_files,
        "test_files": test_files,
        "generated_files": generated_files,
        "count_tests": count_tests,
        "count_comments": count_comments,
        "max_commit_lines": max_commit_lines,
        "max_commit_files": max_commit_files,
    }


def _compute_staged_slice_metrics(
    repo: Path,
    staged: dict[str, str],
    sizing_cfg: SizingConfig,
    *,
    head_exists: bool,
) -> tuple[dict, list[str], list[str], int, list[str]]:
    """Compute staged slice size metrics, non-blocking size/scope warnings, and pairing inputs."""
    total_stats = LineStats()
    production_files = 0
    test_files = 0
    generated_files = 0
    staged_eff_list: list[tuple[int, str]] = []
    staged_prod_paths: list[str] = []
    exec_code_logic_lines = 0
    exec_code_files: list[str] = []

    for rel_path, _status_code in sorted(staged.items()):
        role = classify_file_role(rel_path, config=sizing_cfg)
        if role == "generated":
            generated_files += 1
        elif role == "test":
            test_files += 1
        else:
            production_files += 1
            staged_prod_paths.append(rel_path)

        if (repo / rel_path).is_symlink():
            fstats = LineStats(file_role=role)
        elif head_exists:
            fstats = _compute_file_diff_stats(
                repo, rel_path, cached=True, sizing_config=sizing_cfg
            )
        else:
            fstats = _stats_from_full_text(
                rel_path,
                _read_git_blob_text_safe(repo, f":{rel_path}"),
                config=sizing_cfg,
            )

        total_stats.add_other(fstats)
        eff = fstats.effective_lines(
            count_tests=sizing_cfg.count_tests, count_comments=sizing_cfg.count_comments
        )
        staged_eff_list.append((eff, rel_path))

        if (
            _status_code != "D"
            and role == "production"
            and Path(rel_path).suffix.lower() in EXECUTABLE_CODE_EXTS
            and fstats.raw_additions > 0
            and fstats.logic_lines > 0
        ):
            exec_code_logic_lines += fstats.logic_lines
            exec_code_files.append(rel_path)

    total_effective = sum(eff for eff, _ in staged_eff_list)
    size_metrics = _build_size_metrics_dict(
        total_stats,
        effective_lines=total_effective,
        production_files=production_files,
        test_files=test_files,
        generated_files=generated_files,
        count_tests=sizing_cfg.count_tests,
        count_comments=sizing_cfg.count_comments,
        max_commit_lines=sizing_cfg.max_commit_lines,
        max_commit_files=sizing_cfg.max_commit_files,
    )

    warnings: list[str] = []
    if sizing_cfg.max_commit_lines > 0 and total_effective > sizing_cfg.max_commit_lines:
        top3 = sorted(
            ((eff, path) for eff, path in staged_eff_list if eff > 0),
            key=lambda item: (-item[0], item[1]),
        )[:3]
        top_contributors = ", ".join(f"{path} ({eff} lines)" for eff, path in top3)
        warnings.append(
            f"Commit size warning: staged slice has {total_effective} effective lines "
            f"(target <= {sizing_cfg.max_commit_lines}; tests={'included' if sizing_cfg.count_tests else 'excluded'}, "
            f"comments={'included' if sizing_cfg.count_comments else 'excluded'}). "
            f"Largest contributors: {top_contributors}. "
            f"Consider splitting functions/hunks bottom-up via `hunks`/`chunks` + `stage --lines` "
            f"(keep whole only when symbols are mutually coupled)."
        )

    if sizing_cfg.max_commit_files > 0 and production_files > sizing_cfg.max_commit_files:
        warnings.append(
            f"Commit scope warning: staged slice touches {production_files} production files "
            f"(target <= {sizing_cfg.max_commit_files}; excludes test & generated files). "
            f"Consider narrowing this commit to a smaller vertical/layer slice if concerns are separable."
        )

    return size_metrics, warnings, staged_prod_paths, exec_code_logic_lines, exec_code_files

def verify_staged_slice(
    repo: Path,
    commands: list[str] | None = None,
    *,
    skip_worktree: bool = False,
    command_timeout: int = 120,
) -> dict:
    """Deterministically verify that the staged slice (`HEAD + index`) is valid and self-contained."""
    import ledger as asset_ledger

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

    effective_ledger = None
    ledger_error: str | None = None
    try:
        effective_ledger = _load_effective_ledger_for_verify(repo, staged)
    except (asset_ledger.LedgerCorruptError, json.JSONDecodeError, ValueError) as exc:
        ledger_error = str(exc)

    staged_assets = [
        p
        for p, code in sorted(staged.items())
        if code != "D"
        and not (repo / p).is_symlink()
        and Path(p).suffix.lower() in asset_ledger.ASSET_EXTENSIONS
        and not asset_ledger._is_ignored_path(p, asset_ledger.load_ignore_patterns(repo))
    ]
    if ledger_error and (staged_assets or asset_ledger.LEDGER_REL_PATH in staged):
        errors.append(f"Asset provenance ledger is invalid: {ledger_error}")

    # Determine which files have unstaged modifications in the working tree
    raw_unstaged = _run_git_bytes(["diff", "--name-only", "-z"], repo)
    unstaged_modified = {
        tok.decode("utf-8", errors="surrogateescape")
        for tok in raw_unstaged.split(b"\x00")
        if tok
    }

    # Check 1 & 2: Hygiene + Static Index-vs-Worktree Dependency Isolation
    for rel_path, status_code in sorted(staged.items()):
        if status_code == "D" or (repo / rel_path).is_symlink():
            continue

        # Check asset ledger compliance for staged images/media
        if rel_path in staged_assets:
            if effective_ledger is not None:
                entry = effective_ledger.get("assets", {}).get(
                    asset_ledger.normalize_asset_path(rel_path)
                )
                if not entry or entry.get("status") != "active":
                    errors.append(
                        f"Staged asset '{rel_path}' is missing active provenance in "
                        f"docs/assets_ledger.json (must be logged via log_asset_provenance)."
                    )
            continue

        staged_content = read_index_blob(repo, rel_path)
        if staged_content is None:
            continue

        # Check conflict markers
        if CONFLICT_MARKER_RE.search(staged_content):
            errors.append(f"Staged file '{rel_path}' contains leftover merge conflict markers.")

        # Check imports against unstaged/untracked files and partially staged exports
        imports = extract_file_imports(rel_path, staged_content, known_files, repo=repo)
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

            # Symlink dependency caches (root + subpackage node_modules, .venv) into ephemeral worktree
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

            for cmd_str in commands:
                try:
                    proc = subprocess.run(
                        cmd_str,
                        cwd=str(tmp_dir),
                        shell=True,
                        text=True,
                        capture_output=True,
                        timeout=command_timeout,
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
                except subprocess.TimeoutExpired as exc:
                    command_results.append(
                        {
                            "command": cmd_str,
                            "returncode": 124,
                            "stdout": exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                            "stderr": f"Command timed out after {command_timeout}s",
                        }
                    )
                    errors.append(
                        f"Verification command timed out after {command_timeout}s in isolated staged worktree: {cmd_str}"
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
    p_verify.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Per-command timeout in seconds inside the isolated worktree (default: 120).",
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
            command_timeout=args.timeout,
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
