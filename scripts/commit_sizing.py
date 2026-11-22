#!/usr/bin/env python3
"""Source-line classification and commit/PR sizing helpers for atomic-commit-chain."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
from typing import Any

GOVERNANCE_CONFIG_REL_PATH = ".crenet-governance.json"
DEFAULT_MAX_COMMIT_LOGIC_LINES = 200
DEFAULT_MAX_COMMIT_FILES = 10
DEFAULT_MAX_PR_LOGIC_LINES = 400

GENERATED_BASENAMES = frozenset(
    {
        "uv.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "poetry.lock",
        "Pipfile.lock",
        "go.sum",
        "composer.lock",
        "assets_ledger.json",
        "coverage.json",
        "coverage-summary.json",
        "coverage-final.json",
        "coverage.xml",
        "lcov.info",
    }
)
GENERATED_EXTENSIONS = frozenset(
    {
        ".lock",
        ".snap",
        ".map",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".ico",
        ".glb",
        ".gltf",
        ".vrm",
        ".blend",
        ".wav",
        ".mp3",
        ".ogg",
        ".woff",
        ".woff2",
        ".ttf",
    }
)
TEST_DIR_SEGMENTS = frozenset(
    {"test", "tests", "__tests__", "__mocks__", "e2e", "cypress", "playwright"}
)

PY_DOCSTRING_EXTS = frozenset({".py", ".pyi"})
C_STYLE_COMMENT_EXTS = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".mts",
        ".cts",
        ".css",
        ".scss",
        ".less",
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
        ".jsonc",
    }
)
HTML_COMMENT_EXTS = frozenset({".html", ".htm", ".xml", ".svg", ".md", ".vue", ".svelte"})
HASH_COMMENT_EXTS = frozenset(
    {".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".rb", ".pl", ".mk"}
)
HASH_COMMENT_BASENAMES = frozenset(
    {"Makefile", "Dockerfile", ".gitignore", ".dockerignore", ".aiignore"}
)


@dataclass(frozen=True)
class SizingConfig:
    """Configurable commit/PR sizing budgets and custom generated/test path rules."""

    max_commit_lines: int = DEFAULT_MAX_COMMIT_LOGIC_LINES
    max_commit_files: int = DEFAULT_MAX_COMMIT_FILES
    max_pr_lines: int = DEFAULT_MAX_PR_LOGIC_LINES
    count_tests: bool = False
    count_comments: bool = False
    generated_basenames: frozenset[str] = field(default_factory=frozenset)
    generated_extensions: frozenset[str] = field(default_factory=frozenset)
    generated_globs: tuple[str, ...] = ()
    test_globs: tuple[str, ...] = ()


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a repo-relative glob pattern supporting `**`, `*`, and `?` into a regex."""
    regex_parts: list[str] = ["^"]
    idx = 0
    n = len(pattern)
    while idx < n:
        if pattern.startswith("**/", idx):
            regex_parts.append("(?:.*/)?")
            idx += 3
        elif pattern.startswith("**", idx):
            regex_parts.append(".*")
            idx += 2
        elif pattern[idx] == "*":
            regex_parts.append("[^/]*")
            idx += 1
        elif pattern[idx] == "?":
            regex_parts.append("[^/]")
            idx += 1
        else:
            regex_parts.append(re.escape(pattern[idx]))
            idx += 1
    regex_parts.append("$")
    return re.compile("".join(regex_parts))


def _matches_repo_glob(rel_posix: str, pattern: str) -> bool:
    """Return True if `rel_posix` (or its basename for slash-free globs) matches `pattern`."""
    norm_pat = pattern.strip().replace("\\", "/")
    if norm_pat.startswith("./"):
        norm_pat = norm_pat[2:]
    norm_pat = norm_pat.lstrip("/")
    if not norm_pat:
        return False
    rx = _glob_to_regex(norm_pat)
    if rx.match(rel_posix):
        return True
    if "/" not in norm_pat:
        return bool(rx.match(rel_posix.rsplit("/", 1)[-1]))
    return False


def _as_str_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _normalize_ext(raw_ext: str) -> str:
    cleaned = raw_ext.strip().lower()
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def _int_setting(mapping: dict[str, Any], key: str, default: int) -> int:
    val = mapping.get(key)
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return default


def _bool_setting(mapping: dict[str, Any], key: str, default: bool) -> bool:
    val = mapping.get(key)
    if isinstance(val, bool):
        return val
    return default


def _read_sizing_governance_dict(
    repo: Path, staged: dict[str, str] | None = None
) -> dict[str, Any]:
    raw_text: str | None = None
    if staged and GOVERNANCE_CONFIG_REL_PATH in staged and staged[GOVERNANCE_CONFIG_REL_PATH] != "D":
        res = subprocess.run(
            ["git", "show", f":{GOVERNANCE_CONFIG_REL_PATH}"],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )
        if res.returncode == 0 and b"\x00" not in res.stdout:
            raw_text = res.stdout.decode("utf-8", errors="replace")
    if raw_text is None:
        raw_text = _read_text_safe(repo / GOVERNANCE_CONFIG_REL_PATH)
    if not raw_text:
        return {}
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_sizing_config(
    repo: Path,
    staged: dict[str, str] | None = None,
    *,
    max_commit_lines: int | None = None,
    max_commit_files: int | None = None,
    max_pr_lines: int | None = None,
    count_tests: bool | None = None,
    count_comments: bool | None = None,
) -> SizingConfig:
    """Load sizing budgets and custom generated/test rules from `.crenet-governance.json`."""
    cfg_dict = _read_sizing_governance_dict(repo, staged)
    raw_sizing = cfg_dict.get("sizing")
    sizing_dict = raw_sizing if isinstance(raw_sizing, dict) else {}

    eff_max_commit_lines = (
        max_commit_lines
        if max_commit_lines is not None
        else _int_setting(sizing_dict, "max_commit_lines", DEFAULT_MAX_COMMIT_LOGIC_LINES)
    )
    eff_max_commit_files = (
        max_commit_files
        if max_commit_files is not None
        else _int_setting(sizing_dict, "max_commit_files", DEFAULT_MAX_COMMIT_FILES)
    )
    eff_max_pr_lines = (
        max_pr_lines
        if max_pr_lines is not None
        else _int_setting(sizing_dict, "max_pr_lines", DEFAULT_MAX_PR_LOGIC_LINES)
    )
    eff_count_tests = (
        count_tests
        if count_tests is not None
        else _bool_setting(sizing_dict, "count_tests", False)
    )
    eff_count_comments = (
        count_comments
        if count_comments is not None
        else _bool_setting(sizing_dict, "count_comments", False)
    )

    gen_basenames = frozenset(_as_str_tuple(sizing_dict.get("generated_basenames")))
    gen_exts = frozenset(
        norm
        for norm in (_normalize_ext(x) for x in _as_str_tuple(sizing_dict.get("generated_extensions")))
        if norm
    )
    gen_globs = _as_str_tuple(sizing_dict.get("generated_globs"))
    tst_globs = _as_str_tuple(sizing_dict.get("test_globs"))

    return SizingConfig(
        max_commit_lines=eff_max_commit_lines,
        max_commit_files=eff_max_commit_files,
        max_pr_lines=eff_max_pr_lines,
        count_tests=eff_count_tests,
        count_comments=eff_count_comments,
        generated_basenames=gen_basenames,
        generated_extensions=gen_exts,
        generated_globs=gen_globs,
        test_globs=tst_globs,
    )


def classify_file_role(rel_path: str, config: SizingConfig | None = None) -> str:
    """Classify a repository-relative path as 'generated', 'test', or 'production'."""
    rel_posix = rel_path.replace("\\", "/")
    if rel_posix.startswith("./"):
        rel_posix = rel_posix[2:]
    p = Path(rel_posix)
    name = p.name
    lower_name = name.lower()
    suffix = p.suffix.lower()

    if (
        name in GENERATED_BASENAMES
        or lower_name.endswith((".min.js", ".min.css"))
        or suffix in GENERATED_EXTENSIONS
    ):
        return "generated"

    if config is not None and (
        name in config.generated_basenames
        or any(lower_name.endswith(ext) for ext in config.generated_extensions)
        or any(_matches_repo_glob(rel_posix, pat) for pat in config.generated_globs)
    ):
        return "generated"

    dir_parts = [part.lower() for part in p.parts[:-1]]
    if (
        any(seg in TEST_DIR_SEGMENTS for seg in dir_parts)
        or lower_name == "conftest.py"
        or (lower_name.startswith("test_") and suffix == ".py")
        or lower_name.endswith(("_test.py", "_test.go"))
        or ".test." in lower_name
        or ".spec." in lower_name
    ):
        return "test"

    if config is not None and any(_matches_repo_glob(rel_posix, pat) for pat in config.test_globs):
        return "test"

    return "production"


def _count_unescaped(text: str, token: str) -> int:
    """Count non-overlapping, unescaped occurrences of `token` in `text`."""
    count = 0
    idx = 0
    n = len(text)
    tok_len = len(token)
    while idx < n:
        if text[idx] == "\\":
            idx += 2
            continue
        if text.startswith(token, idx):
            count += 1
            idx += tok_len
            continue
        idx += 1
    return count


def _process_c_style_line(
    raw_line: str, in_block: bool, *, fallback_mode: bool = False
) -> tuple[bool, bool]:
    """Scan a single C/JS/TS/CSS-family line returning `(is_comment_only, next_in_block)`."""
    stripped = raw_line.strip()
    if fallback_mode and not in_block and (
        stripped == "*" or stripped.startswith("* ") or stripped.startswith("*/")
    ):
        if stripped.startswith("*/"):
            after = stripped[2:].strip()
            if not after or after.startswith("//"):
                return (True, False)
        else:
            return (True, False)

    n = len(raw_line)
    idx = 0
    in_quote: str | None = None
    saw_code = False

    while idx < n:
        ch = raw_line[idx]
        if in_block:
            if raw_line.startswith("*/", idx):
                in_block = False
                idx += 2
                continue
            idx += 1
            continue

        if in_quote is not None:
            saw_code = True
            if ch == "\\":
                idx += 2
                continue
            if ch == in_quote:
                in_quote = None
            idx += 1
            continue

        if ch in (" ", "\t", "\r", "\n"):
            idx += 1
            continue

        if raw_line.startswith("//", idx):
            break

        if raw_line.startswith("/*", idx):
            in_block = True
            idx += 2
            continue

        if ch in ('"', "'", "`"):
            in_quote = ch
            saw_code = True
            idx += 1
            continue

        saw_code = True
        idx += 1

    return (not saw_code, in_block)


def _process_html_comment_line(raw_line: str, in_block: bool) -> tuple[bool, bool]:
    """Scan an HTML/XML/Markdown line for `<!-- ... -->`, returning `(is_comment_only, next_in_block)`."""
    n = len(raw_line)
    idx = 0
    saw_code = False

    while idx < n:
        if in_block:
            end_idx = raw_line.find("-->", idx)
            if end_idx == -1:
                break
            in_block = False
            idx = end_idx + 3
            continue

        if raw_line[idx] in (" ", "\t", "\r", "\n"):
            idx += 1
            continue

        if raw_line.startswith("<!--", idx):
            in_block = True
            idx += 4
            continue

        saw_code = True
        idx += 1

    return (not saw_code, in_block)


def classify_source_lines(
    rel_path: str, lines: list[str], *, fallback_mode: bool = False
) -> tuple[set[int], set[int]]:
    """Return `(blank_line_nums, comment_line_nums)` using 1-based line numbers."""
    p = Path(rel_path.replace("\\", "/"))
    ext = p.suffix.lower()
    name = p.name

    blank_nums: set[int] = set()
    comment_nums: set[int] = set()

    py_delim: str | None = None
    c_in_block = False
    html_in_block = False

    for lineno, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            blank_nums.add(lineno)
            continue

        if ext in PY_DOCSTRING_EXTS:
            if py_delim is not None:
                comment_nums.add(lineno)
                if _count_unescaped(stripped, py_delim) % 2 == 1:
                    py_delim = None
                continue

            if stripped.startswith("#"):
                if not (lineno == 1 and stripped.startswith("#!")):
                    comment_nums.add(lineno)
                continue

            if stripped.startswith(('"""', "'''", 'r"""', "r'''", 'R"""', "R'''")):
                delim = '"""' if '"""' in stripped[:5] else "'''"
                prefix_end = stripped.find(delim) + 3
                rest = stripped[prefix_end:]
                comment_nums.add(lineno)
                if _count_unescaped(rest, delim) % 2 == 0:
                    py_delim = delim
                continue

        elif ext in C_STYLE_COMMENT_EXTS:
            is_comment, c_in_block = _process_c_style_line(
                raw_line, c_in_block, fallback_mode=fallback_mode
            )
            if is_comment:
                comment_nums.add(lineno)

        elif ext in HTML_COMMENT_EXTS:
            is_comment, html_in_block = _process_html_comment_line(raw_line, html_in_block)
            if is_comment:
                comment_nums.add(lineno)

        elif ext in HASH_COMMENT_EXTS or name in HASH_COMMENT_BASENAMES:
            if stripped.startswith("#") and not (lineno == 1 and stripped.startswith("#!")):
                comment_nums.add(lineno)

    return (blank_nums, comment_nums)


@dataclass
class LineStats:
    file_role: str = "production"
    raw_additions: int = 0
    raw_deletions: int = 0
    logic_lines: int = 0
    test_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    generated_lines: int = 0

    def add_other(self, other: LineStats) -> None:
        self.raw_additions += other.raw_additions
        self.raw_deletions += other.raw_deletions
        self.logic_lines += other.logic_lines
        self.test_lines += other.test_lines
        self.comment_lines += other.comment_lines
        self.blank_lines += other.blank_lines
        self.generated_lines += other.generated_lines

    def effective_lines(self, *, count_tests: bool = False, count_comments: bool = False) -> int:
        if self.file_role == "generated":
            return 0
        if self.file_role == "test":
            if not count_tests:
                return 0
            return self.test_lines + (self.comment_lines if count_comments else 0)
        return self.logic_lines + (self.comment_lines if count_comments else 0)


def _route_line_into_stats(
    stats: LineStats,
    lineno: int,
    blank_nums: set[int],
    comment_nums: set[int],
) -> None:
    if lineno in blank_nums:
        stats.blank_lines += 1
    elif lineno in comment_nums:
        stats.comment_lines += 1
    elif stats.file_role == "test":
        stats.test_lines += 1
    else:
        stats.logic_lines += 1


def compute_hunk_line_stats(
    rel_path: str,
    hunk: Any,
    *,
    old_classified: tuple[set[int], set[int]] | None = None,
    new_classified: tuple[set[int], set[int]] | None = None,
    config: SizingConfig | None = None,
) -> LineStats:
    """Compute categorized line counts for a single parsed DiffHunk."""
    role = classify_file_role(rel_path, config=config)
    stats = LineStats(file_role=role)

    old_lineno = hunk.old_start
    new_lineno = hunk.new_start
    added: list[tuple[int, str]] = []
    removed: list[tuple[int, str]] = []

    for line in hunk.body_lines:
        if line.startswith(" "):
            old_lineno += 1
            new_lineno += 1
        elif line.startswith("+") and not line.startswith("+++"):
            stats.raw_additions += 1
            added.append((new_lineno, line[1:].rstrip("\r\n")))
            new_lineno += 1
        elif line.startswith("-") and not line.startswith("---"):
            stats.raw_deletions += 1
            removed.append((old_lineno, line[1:].rstrip("\r\n")))
            old_lineno += 1

    if role == "generated":
        stats.generated_lines = stats.raw_additions + stats.raw_deletions
        return stats

    if added:
        if new_classified is not None:
            new_blank, new_comment = new_classified
            for lineno, _ in added:
                _route_line_into_stats(stats, lineno, new_blank, new_comment)
        else:
            fb_blank, fb_comment = classify_source_lines(
                rel_path, [txt for _, txt in added], fallback_mode=True
            )
            for idx in range(1, len(added) + 1):
                _route_line_into_stats(stats, idx, fb_blank, fb_comment)

    if removed:
        if old_classified is not None:
            old_blank, old_comment = old_classified
            for lineno, _ in removed:
                _route_line_into_stats(stats, lineno, old_blank, old_comment)
        else:
            fb_blank, fb_comment = classify_source_lines(
                rel_path, [txt for _, txt in removed], fallback_mode=True
            )
            for idx in range(1, len(removed) + 1):
                _route_line_into_stats(stats, idx, fb_blank, fb_comment)

    return stats


def compute_hunk_dict(
    rel_path: str,
    hunk: Any,
    *,
    old_classified: tuple[set[int], set[int]] | None = None,
    new_classified: tuple[set[int], set[int]] | None = None,
    config: SizingConfig | None = None,
) -> dict:
    """Serialize a DiffHunk together with its categorized line statistics."""
    stats = compute_hunk_line_stats(
        rel_path,
        hunk,
        old_classified=old_classified,
        new_classified=new_classified,
        config=config,
    )
    return {
        "index": hunk.index,
        "header": hunk.header.strip(),
        "old_start": hunk.old_start,
        "old_count": hunk.old_count,
        "new_start": hunk.new_start,
        "new_count": hunk.new_count,
        "section_hint": hunk.section_hint,
        "preview": hunk.preview,
        "file_role": stats.file_role,
        "raw_additions": stats.raw_additions,
        "raw_deletions": stats.raw_deletions,
        "logic_lines": stats.logic_lines,
        "test_lines": stats.test_lines,
        "comment_lines": stats.comment_lines,
        "blank_lines": stats.blank_lines,
    }


def _read_text_safe(path: Path) -> str | None:
    """Read a UTF-8 file if it is a regular non-binary file."""
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


def _stats_from_full_text(
    rel_path: str, text: str | None, *, config: SizingConfig | None = None
) -> LineStats:
    """Compute LineStats treating every line of `text` as an added line."""
    role = classify_file_role(rel_path, config=config)
    stats = LineStats(file_role=role)
    if text is None:
        return stats
    lines = text.splitlines()
    stats.raw_additions = len(lines)
    if role == "generated":
        stats.generated_lines = len(lines)
        return stats
    blank_nums, comment_nums = classify_source_lines(rel_path, lines)
    for lineno in range(1, len(lines) + 1):
        _route_line_into_stats(stats, lineno, blank_nums, comment_nums)
    return stats
