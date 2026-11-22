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
