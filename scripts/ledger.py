#!/usr/bin/env python3
"""Crenet Games Asset Provenance Ledger CLI & library (v2 recipe model).

Tracks provenance steps (1..6 steps per asset) and computes AI involvement
(`none` | `assisted` | `generated`) across both tracked repository assets
and Tier 1 workspace intermediates under `artifacts/`.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 2

LEDGER_REL_PATH = "docs/assets_ledger.json"
PROJECT_CONFIG_REL_PATH = ".crenet-governance.json"
LEDGER_IGNORE_REL_PATH = ".assetledgerignore"

ASSET_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".svg",
        ".gif",
        ".bmp",
        ".ico",
        ".avif",
        ".mp4",
        ".webm",
        ".wav",
        ".mp3",
        ".ogg",
    }
)

IGNORED_DIR_PARTS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "coverage",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".next",
        ".svelte-kit",
        ".cache",
        "tmp",
        ".turbo",
    }
)

VALID_STATUSES: frozenset[str] = frozenset({"active", "deprecated"})
VALID_ACTORS: frozenset[str] = frozenset({"human", "ai", "procedural"})
VALID_AI_INVOLVEMENT: frozenset[str] = frozenset({"none", "assisted", "generated"})

ENTRY_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "asset_path",
        "previous_paths",
        "status",
        "ai_involvement",
        "sha256",
        "notes",
        "logged_at",
        "updated_at",
        "steps",
    }
)
ENTRY_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"asset_path", "status", "ai_involvement", "steps", "logged_at"}
)

STEP_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"actor", "tool", "summary", "prompt", "artifact_ref", "sha256", "seed", "license"}
)
STEP_REQUIRED_KEYS: frozenset[str] = frozenset({"actor", "tool", "summary"})

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

MIN_STEPS = 1
MAX_STEPS = 6


class LedgerCorruptError(ValueError):
    """Raised when the asset ledger or governance config fails validation."""


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_asset_path(raw_path: str | Path, repo_root: Path | None = None) -> str:
    """Normalize an asset path to a repository-relative POSIX path."""
    path_str = str(raw_path).strip().replace("\\", "/")
    if not path_str:
        raise ValueError("Asset path must be a non-empty string.")

    if repo_root is not None:
        resolved_root = repo_root.resolve()
        candidate = Path(path_str)
        if candidate.is_absolute():
            try:
                rel = candidate.resolve().relative_to(resolved_root)
            except ValueError as exc:
                raise ValueError(
                    f"Path {path_str!r} is outside repository root {resolved_root}"
                ) from exc
            path_str = rel.as_posix()

    if path_str.startswith("/"):
        raise ValueError(f"Asset path must be relative to the repository root: {path_str!r}")

    normalized = os.path.normpath(path_str).replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"Invalid repository-relative asset path: {raw_path!r}")
    return normalized


def compute_sha256(file_path: Path) -> str:
    """Compute lowercase SHA-256 hex digest of a file on disk."""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_ai_involvement(steps: list[dict[str, Any]]) -> str:
    """Derive ai_involvement ('none' | 'assisted' | 'generated') from steps."""
    if not isinstance(steps, list) or not (MIN_STEPS <= len(steps) <= MAX_STEPS):
        raise ValueError(f"steps must contain between {MIN_STEPS} and {MAX_STEPS} items.")
    actors = {step.get("actor") for step in steps}
    if not actors.issubset(VALID_ACTORS):
        raise ValueError(f"Invalid actor in steps: {actors - VALID_ACTORS}")
    if "ai" not in actors:
        return "none"
    if actors == {"ai"}:
        return "generated"
    return "assisted"


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) > 0


def validate_step(step: Any, index: int, entry_key: str) -> None:
    """Validate one provenance step against the Draft-07 schema rules."""
    if not isinstance(step, dict):
        raise LedgerCorruptError(f"{entry_key}.steps[{index}] must be an object.")
    extra = set(step.keys()) - STEP_ALLOWED_KEYS
    if extra:
        raise LedgerCorruptError(
            f"{entry_key}.steps[{index}] has unexpected fields: {sorted(extra)}"
        )
    missing = STEP_REQUIRED_KEYS - set(step.keys())
    if missing:
        raise LedgerCorruptError(
            f"{entry_key}.steps[{index}] missing required fields: {sorted(missing)}"
        )
    if step["actor"] not in VALID_ACTORS:
        raise LedgerCorruptError(
            f"{entry_key}.steps[{index}].actor must be one of {sorted(VALID_ACTORS)}"
        )
    for str_field in ("tool", "summary"):
        if not _is_nonempty_str(step[str_field]):
            raise LedgerCorruptError(
                f"{entry_key}.steps[{index}].{str_field} must be a non-empty string."
            )

    if step["actor"] == "ai":
        if not _is_nonempty_str(step.get("prompt")):
            raise LedgerCorruptError(
                f"{entry_key}.steps[{index}].prompt is required when actor == 'ai'."
            )
    elif "prompt" in step:
        raise LedgerCorruptError(
            f"{entry_key}.steps[{index}].prompt is only allowed when actor == 'ai'."
        )

    for opt_str in ("artifact_ref", "license"):
        if opt_str in step and not _is_nonempty_str(step[opt_str]):
            raise LedgerCorruptError(
                f"{entry_key}.steps[{index}].{opt_str} must be a non-empty string."
            )
    if "sha256" in step:
        sha = step["sha256"]
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            raise LedgerCorruptError(
                f"{entry_key}.steps[{index}].sha256 must be a 64-char lowercase hex digest."
            )
    if "seed" in step:
        seed = step["seed"]
        if isinstance(seed, bool) or not isinstance(seed, (str, int)):
            raise LedgerCorruptError(
                f"{entry_key}.steps[{index}].seed must be a string or integer."
            )


def validate_ledger_data(data: Any) -> None:
    """Validate an in-memory ledger dictionary strictly; raise LedgerCorruptError on failure."""
    if not isinstance(data, dict):
        raise LedgerCorruptError("Asset ledger root must be a JSON object.")

    extra_root = set(data.keys()) - {"version", "assets"}
    if extra_root:
        raise LedgerCorruptError(
            f"Asset ledger root contains unexpected keys: {sorted(extra_root)}. "
            "Run `scripts/ledger.py migrate` if this is a legacy v1 ledger."
        )
    version = data.get("version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise LedgerCorruptError(
            f"Asset ledger must declare version == {SCHEMA_VERSION} (got {version!r}). "
            "Run `scripts/ledger.py migrate` to upgrade legacy v1 ledgers."
        )

    assets = data.get("assets")
    if not isinstance(assets, dict):
        raise LedgerCorruptError("Asset ledger 'assets' field must be a JSON object.")

    for key, entry in assets.items():
        if not isinstance(key, str) or not key:
            raise LedgerCorruptError(f"Ledger asset key must be a non-empty string: {key!r}")
        try:
            normalized_key = normalize_asset_path(key)
        except ValueError as exc:
            raise LedgerCorruptError(f"Invalid ledger asset key {key!r}: {exc}") from exc
        if normalized_key != key:
            raise LedgerCorruptError(
                f"Ledger key {key!r} is not normalized (expected {normalized_key!r})."
            )
        if not isinstance(entry, dict):
            raise LedgerCorruptError(f"Ledger entry for {key!r} must be an object.")
        extra = set(entry.keys()) - ENTRY_ALLOWED_KEYS
        if extra:
            raise LedgerCorruptError(
                f"Ledger entry {key!r} has unexpected keys: {sorted(extra)}"
            )
        missing = ENTRY_REQUIRED_KEYS - set(entry.keys())
        if missing:
            raise LedgerCorruptError(
                f"Ledger entry {key!r} missing required keys: {sorted(missing)}"
            )
        if entry["asset_path"] != key:
            raise LedgerCorruptError(
                f"Ledger entry {key!r} asset_path ({entry['asset_path']!r}) must match its map key."
            )
        if entry["status"] not in VALID_STATUSES:
            raise LedgerCorruptError(
                f"Ledger entry {key!r} invalid status {entry['status']!r}."
            )
        if not _is_nonempty_str(entry.get("logged_at")):
            raise LedgerCorruptError(f"Ledger entry {key!r} logged_at must be non-empty.")
        if "updated_at" in entry and not _is_nonempty_str(entry["updated_at"]):
            raise LedgerCorruptError(f"Ledger entry {key!r} updated_at must be non-empty.")
        if "notes" in entry and not isinstance(entry["notes"], str):
            raise LedgerCorruptError(f"Ledger entry {key!r} notes must be a string.")
        if "sha256" in entry:
            sha = entry["sha256"]
            if not isinstance(sha, str) or not SHA256_RE.match(sha):
                raise LedgerCorruptError(
                    f"Ledger entry {key!r} sha256 must be 64 lowercase hex characters."
                )
        if "previous_paths" in entry:
            prev = entry["previous_paths"]
            if not isinstance(prev, list) or len(set(prev)) != len(prev):
                raise LedgerCorruptError(
                    f"Ledger entry {key!r} previous_paths must be a list of unique strings."
                )
            for item in prev:
                if not _is_nonempty_str(item):
                    raise LedgerCorruptError(
                        f"Ledger entry {key!r} previous_paths cannot contain empty strings."
                    )

        steps = entry["steps"]
        if not isinstance(steps, list) or not (MIN_STEPS <= len(steps) <= MAX_STEPS):
            raise LedgerCorruptError(
                f"Ledger entry {key!r} steps must contain {MIN_STEPS}..{MAX_STEPS} items."
            )
        for idx, step in enumerate(steps):
            validate_step(step, idx, key)

        expected_ai = compute_ai_involvement(steps)
        if entry["ai_involvement"] != expected_ai:
            raise LedgerCorruptError(
                f"Ledger entry {key!r} ai_involvement={entry['ai_involvement']!r} "
                f"does not match computed {expected_ai!r}."
            )


def get_project_config(repo_root: Path) -> dict[str, Any]:
    """Load optional .crenet-governance.json from repo_root."""
    cfg_path = repo_root / PROJECT_CONFIG_REL_PATH
    if not cfg_path.exists():
        return {"asset_provenance": True}
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerCorruptError(
            f"Malformed JSON in {cfg_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise LedgerCorruptError(f"{cfg_path} must contain a JSON object.")
    merged = {"asset_provenance": True, **raw}
    if not isinstance(merged["asset_provenance"], bool):
        raise LedgerCorruptError(
            f"{cfg_path} 'asset_provenance' must be a boolean."
        )
    return merged


def is_asset_provenance_enabled(repo_root: Path) -> bool:
    """Return False when a repo explicitly opts out via .crenet-governance.json."""
    return bool(get_project_config(repo_root)["asset_provenance"])


def ledger_file_path(repo_root: Path) -> Path:
    """Return canonical ledger path inside repo_root."""
    return repo_root / LEDGER_REL_PATH


def load_ledger(repo_root: Path) -> dict[str, Any]:
    """Load and validate docs/assets_ledger.json, returning empty v2 if absent."""
    path = ledger_file_path(repo_root)
    if not path.exists():
        return {"version": SCHEMA_VERSION, "assets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerCorruptError(f"Malformed JSON in {path}: {exc}") from exc
    validate_ledger_data(data)
    return data


def save_ledger(repo_root: Path, data: dict[str, Any]) -> Path:
    """Validate and atomically write docs/assets_ledger.json with sorted keys."""
    ordered_assets = {k: data["assets"][k] for k in sorted(data["assets"].keys())}
    payload = {"version": SCHEMA_VERSION, "assets": ordered_assets}
    validate_ledger_data(payload)

    target = ledger_file_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.tmp.{os.getpid()}"
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, target)
    return target


def load_ignore_patterns(repo_root: Path) -> list[str]:
    """Read optional .assetledgerignore glob patterns."""
    ignore_path = repo_root / LEDGER_IGNORE_REL_PATH
    if not ignore_path.exists():
        return []
    patterns: list[str] = []
    for line in ignore_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped.rstrip("/"))
    return patterns


def _is_ignored_path(rel_path: str, ignore_patterns: list[str]) -> bool:
    parts = PurePosixPath(rel_path).parts
    if any(part in IGNORED_DIR_PARTS for part in parts):
        return True
    name = PurePosixPath(rel_path).name
    for pattern in ignore_patterns:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def discover_repo_assets(repo_root: Path) -> list[str]:
    """Discover active visual/audio asset files in repo (including gitignored artifacts/)."""
    candidates: set[str] = set()
    resolved_root = repo_root.resolve()

    if (resolved_root / ".git").exists() or (
        subprocess.run(
            ["git", "-C", str(resolved_root), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    ):
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(resolved_root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
        )
        if proc.returncode == 0:
            for raw in proc.stdout.split(b"\x00"):
                if raw:
                    candidates.add(raw.decode("utf-8", errors="surrogateescape"))

        artifacts_dir = resolved_root / "artifacts"
        if artifacts_dir.is_dir():
            for file_path in artifacts_dir.rglob("*"):
                if file_path.is_file():
                    candidates.add(file_path.relative_to(resolved_root).as_posix())
    else:
        for file_path in resolved_root.rglob("*"):
            if file_path.is_file():
                candidates.add(file_path.relative_to(resolved_root).as_posix())

    ignore_patterns = load_ignore_patterns(resolved_root)
    results: list[str] = []
    for rel in sorted(candidates):
        if PurePosixPath(rel).suffix.lower() not in ASSET_EXTENSIONS:
            continue
        if _is_ignored_path(rel, ignore_patterns):
            continue
        if not (resolved_root / rel).is_file():
            continue
        results.append(rel)
    return results


def _normalize_steps(
    steps: list[dict[str, Any]], repo_root: Path
) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not (MIN_STEPS <= len(steps) <= MAX_STEPS):
        raise ValueError(f"steps must be a list of {MIN_STEPS}..{MAX_STEPS} objects.")
    cleaned: list[dict[str, Any]] = []
    for idx, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            raise ValueError(f"steps[{idx}] must be an object.")
        step = dict(raw_step)
        if step.get("actor") != "ai" and "prompt" in step:
            if step["prompt"] in ("", None):
                step.pop("prompt", None)
        if "artifact_ref" in step and _is_nonempty_str(step["artifact_ref"]):
            ref_norm = normalize_asset_path(step["artifact_ref"], repo_root)
            step["artifact_ref"] = ref_norm
            ref_disk = repo_root / ref_norm
            if ref_disk.is_file() and "sha256" not in step:
                step["sha256"] = compute_sha256(ref_disk)
        cleaned.append(step)
    return cleaned


def log_asset(
    repo_root: Path,
    asset_path: str,
    steps: list[dict[str, Any]],
    *,
    status: str = "active",
    notes: str | None = None,
    previous_paths: list[str] | None = None,
    append_steps: bool = False,
) -> dict[str, Any]:
    """Create or update an asset provenance entry in docs/assets_ledger.json."""
    resolved_root = repo_root.resolve()
    norm_path = normalize_asset_path(asset_path, resolved_root)
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")

    ledger = load_ledger(resolved_root)
    existing = ledger["assets"].get(norm_path)

    normalized_steps = _normalize_steps(steps, resolved_root)
    if append_steps and existing is not None:
        combined = existing["steps"] + normalized_steps
        if len(combined) > MAX_STEPS:
            raise ValueError(
                f"Appending steps exceeds maximum of {MAX_STEPS} steps ({len(combined)})."
            )
        final_steps = combined
    else:
        final_steps = normalized_steps

    ai_involvement = compute_ai_involvement(final_steps)
    now = utc_now_iso()

    merged_prev: list[str] = []
    for candidate in (existing.get("previous_paths", []) if existing else []) + (
        previous_paths or []
    ):
        norm_prev = normalize_asset_path(candidate, resolved_root)
        if norm_prev != norm_path and norm_prev not in merged_prev:
            merged_prev.append(norm_prev)

    entry: dict[str, Any] = {
        "asset_path": norm_path,
        "status": status,
        "ai_involvement": ai_involvement,
        "steps": final_steps,
        "logged_at": existing["logged_at"] if existing else now,
    }
    if existing is not None:
        entry["updated_at"] = now
    if merged_prev:
        entry["previous_paths"] = merged_prev

    resolved_notes = notes if notes is not None else (existing.get("notes") if existing else None)
    if resolved_notes is not None:
        entry["notes"] = resolved_notes

    disk_file = resolved_root / norm_path
    if disk_file.is_file():
        entry["sha256"] = compute_sha256(disk_file)
    elif existing and "sha256" in existing:
        entry["sha256"] = existing["sha256"]

    ledger["assets"][norm_path] = entry
    save_ledger(resolved_root, ledger)
    return entry


def move_asset(
    repo_root: Path,
    old_path: str,
    new_path: str,
    *,
    step: dict[str, Any] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Graduate or relocate an asset entry, preserving provenance and updating previous_paths."""
    resolved_root = repo_root.resolve()
    old_norm = normalize_asset_path(old_path, resolved_root)
    new_norm = normalize_asset_path(new_path, resolved_root)
    if old_norm == new_norm:
        raise ValueError("old_path and new_path refer to the same repository path.")

    ledger = load_ledger(resolved_root)
    if old_norm not in ledger["assets"]:
        raise KeyError(f"Asset {old_norm!r} not found in ledger.")

    existing = ledger["assets"].pop(old_norm)
    steps = list(existing["steps"])
    if step is not None:
        norm_new_steps = _normalize_steps([step], resolved_root)
        if len(steps) < MAX_STEPS:
            steps.extend(norm_new_steps)
        else:
            extra_note = f"Moved {old_norm} -> {new_norm} ({step.get('summary', '')})"
            notes = f"{notes}; {extra_note}" if notes else extra_note

    prev_paths: list[str] = []
    for item in list(existing.get("previous_paths", [])) + [old_norm]:
        if item != new_norm and item not in prev_paths:
            prev_paths.append(item)

    now = utc_now_iso()
    entry: dict[str, Any] = {
        "asset_path": new_norm,
        "previous_paths": prev_paths,
        "status": "active",
        "ai_involvement": compute_ai_involvement(steps),
        "steps": steps,
        "logged_at": existing["logged_at"],
        "updated_at": now,
    }
    combined_notes = notes if notes is not None else existing.get("notes")
    if combined_notes is not None:
        entry["notes"] = combined_notes

    disk_file = resolved_root / new_norm
    if disk_file.is_file():
        entry["sha256"] = compute_sha256(disk_file)
    elif "sha256" in existing:
        entry["sha256"] = existing["sha256"]

    ledger["assets"][new_norm] = entry
    save_ledger(resolved_root, ledger)
    return entry


def _hash_active_files(repo_root: Path, paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel in paths:
        disk_path = repo_root / rel
        if disk_path.is_file():
            hashes[rel] = compute_sha256(disk_path)
    return hashes


def sync_moves(
    repo_root: Path, *, dry_run: bool = False
) -> dict[str, Any]:
    """Match missing active ledger entries against unlogged disk files by SHA-256."""
    resolved_root = repo_root.resolve()
    ledger = load_ledger(resolved_root)
    disk_assets = set(discover_repo_assets(resolved_root))
    assets = ledger["assets"]

    orphan_by_sha: dict[str, list[str]] = {}
    for path, entry in assets.items():
        if entry["status"] == "active" and not (resolved_root / path).is_file():
            sha = entry.get("sha256")
            if sha:
                orphan_by_sha.setdefault(sha, []).append(path)

    unlogged = sorted(
        p for p in disk_assets if p not in assets or assets[p]["status"] != "active"
    )
    unlogged_by_sha: dict[str, list[str]] = {}
    for path in unlogged:
        sha = compute_sha256(resolved_root / path)
        unlogged_by_sha.setdefault(sha, []).append(path)

    moved: list[dict[str, str]] = []
    ambiguous: list[dict[str, Any]] = []
    for sha, new_paths in unlogged_by_sha.items():
        old_paths = orphan_by_sha.get(sha, [])
        if not old_paths:
            continue
        if len(old_paths) == 1 and len(new_paths) == 1:
            old_p, new_p = old_paths[0], new_paths[0]
            if not dry_run:
                move_asset(
                    resolved_root,
                    old_p,
                    new_p,
                    step={
                        "actor": "procedural",
                        "tool": "ledger-sync-moves",
                        "summary": f"Relocated matching SHA-256 asset from {old_p}.",
                    },
                )
            moved.append({"from": old_p, "to": new_p, "sha256": sha})
        else:
            ambiguous.append({"sha256": sha, "from": old_paths, "to": new_paths})

    return {"moved": moved, "ambiguous": ambiguous, "dry_run": dry_run}


def verify_ledger(
    repo_root: Path,
    *,
    paths: list[str] | None = None,
    strict_hashes: bool = False,
) -> dict[str, Any]:
    """Audit disk assets against docs/assets_ledger.json and return structured findings."""
    resolved_root = repo_root.resolve()
    if not is_asset_provenance_enabled(resolved_root):
        return {
            "enabled": False,
            "ok": True,
            "unlogged": [],
            "orphan_entries": [],
            "stale_hashes": [],
            "graduation_candidates": [],
            "total_disk_assets": 0,
            "total_ledger_entries": 0,
        }

    ledger = load_ledger(resolved_root)
    assets = ledger["assets"]
    disk_assets = discover_repo_assets(resolved_root)

    if paths is not None:
        check_paths = sorted(
            {
                normalize_asset_path(p, resolved_root)
                for p in paths
                if PurePosixPath(str(p)).suffix.lower() in ASSET_EXTENSIONS
                and (resolved_root / normalize_asset_path(p, resolved_root)).is_file()
                and not _is_ignored_path(
                    normalize_asset_path(p, resolved_root),
                    load_ignore_patterns(resolved_root),
                )
            }
        )
    else:
        check_paths = disk_assets

    unlogged: list[str] = []
    stale_hashes: list[dict[str, str]] = []
    unlogged_shas: dict[str, str] = {}

    for rel in check_paths:
        entry = assets.get(rel)
        if entry is None or entry["status"] != "active":
            unlogged.append(rel)
            unlogged_shas[rel] = compute_sha256(resolved_root / rel)
            continue
        stored_sha = entry.get("sha256")
        if stored_sha:
            current_sha = compute_sha256(resolved_root / rel)
            if current_sha != stored_sha:
                stale_hashes.append(
                    {"asset_path": rel, "expected": stored_sha, "actual": current_sha}
                )

    orphan_entries: list[str] = []
    if paths is None:
        for rel, entry in sorted(assets.items()):
            if entry["status"] == "active" and not (resolved_root / rel).is_file():
                orphan_entries.append(rel)

    sha_to_ledger: dict[str, list[str]] = {}
    for rel, entry in assets.items():
        if entry.get("sha256"):
            sha_to_ledger.setdefault(entry["sha256"], []).append(rel)

    graduation_candidates: list[dict[str, Any]] = []
    for rel in unlogged:
        matches = sha_to_ledger.get(unlogged_shas[rel], [])
        if matches:
            graduation_candidates.append(
                {
                    "unlogged_path": rel,
                    "matching_ledger_paths": matches,
                    "sha256": unlogged_shas[rel],
                    "orphan_match": any(m in orphan_entries for m in matches),
                }
            )

    ok = len(unlogged) == 0 and (paths is not None or len(orphan_entries) == 0)
    if strict_hashes and stale_hashes:
        ok = False

    return {
        "enabled": True,
        "ok": ok,
        "unlogged": unlogged,
        "orphan_entries": orphan_entries,
        "stale_hashes": stale_hashes,
        "graduation_candidates": graduation_candidates,
        "total_disk_assets": len(disk_assets),
        "total_ledger_entries": len(assets),
    }


def generate_report(repo_root: Path) -> dict[str, Any]:
    """Build a Publisher / M&A Audit Scorecard summarizing provenance coverage."""
    resolved_root = repo_root.resolve()
    if not is_asset_provenance_enabled(resolved_root):
        return {"enabled": False, "repo_root": str(resolved_root)}

    ledger = load_ledger(resolved_root)
    verify = verify_ledger(resolved_root)
    assets = ledger["assets"]

    by_involvement = {"none": 0, "assisted": 0, "generated": 0}
    by_status = {"active": 0, "deprecated": 0}
    artifacts_count = 0
    published_count = 0
    missing_hashes: list[str] = []
    tools_used: set[str] = set()
    actors_used: set[str] = set()

    for rel, entry in sorted(assets.items()):
        by_status[entry["status"]] += 1
        if entry["status"] == "active":
            by_involvement[entry["ai_involvement"]] += 1
            if rel.startswith("artifacts/"):
                artifacts_count += 1
            else:
                published_count += 1
            if "sha256" not in entry:
                missing_hashes.append(rel)
        for step in entry["steps"]:
            tools_used.add(step["tool"])
            actors_used.add(step["actor"])

    return {
        "enabled": True,
        "repo_root": str(resolved_root),
        "schema_version": SCHEMA_VERSION,
        "ok": verify["ok"],
        "counts": {
            "total_entries": len(assets),
            "active": by_status["active"],
            "deprecated": by_status["deprecated"],
            "tier1_artifacts_active": artifacts_count,
            "published_active": published_count,
            "ai_involvement": by_involvement,
        },
        "actors": sorted(actors_used),
        "tools": sorted(tools_used),
        "unlogged": verify["unlogged"],
        "orphan_entries": verify["orphan_entries"],
        "stale_hashes": verify["stale_hashes"],
        "missing_hashes": missing_hashes,
        "graduation_candidates": verify["graduation_candidates"],
    }


def format_report_markdown(report: dict[str, Any]) -> str:
    """Render report dict into a Publisher / M&A Audit Scorecard Markdown summary."""
    if not report.get("enabled", True):
        return (
            "# Asset Provenance Scorecard\n\n"
            "Status: **DISABLED** via `.crenet-governance.json` (`\"asset_provenance\": false`).\n"
        )
    c = report["counts"]
    ai = c["ai_involvement"]
    status_label = "PASS" if report["ok"] else "ACTION REQUIRED"
    lines = [
        "# Asset Provenance Scorecard (M&A / Publisher Audit)",
        "",
        f"- **Audit Status**: `{status_label}`",
        f"- **Schema Version**: `v{report['schema_version']}`",
        f"- **Active Assets**: `{c['active']}` (Published: `{c['published_active']}`, Tier 1 `artifacts/`: `{c['tier1_artifacts_active']}`, Deprecated: `{c['deprecated']}`)",
        "",
        "## AI Involvement Breakdown (Active Assets)",
        "",
        "| Classification | Count | Definition |",
        "| --- | ---: | --- |",
        f"| `none` | {ai['none']} | Zero AI steps (pure human and/or procedural authorship) |",
        f"| `assisted` | {ai['assisted']} | Mixed AI + human/procedural steps |",
        f"| `generated` | {ai['generated']} | Exclusively AI steps |",
        "",
        f"- **Tools Observed**: {', '.join(f'`{t}`' for t in report['tools']) or 'None'}",
        f"- **Unlogged Disk Assets**: `{len(report['unlogged'])}`",
        f"- **Orphan Active Entries**: `{len(report['orphan_entries'])}`",
        f"- **Stale SHA-256 Digests**: `{len(report['stale_hashes'])}`",
    ]
    if report["unlogged"]:
        lines.extend(["", "### Unlogged Assets"] + [f"- `{p}`" for p in report["unlogged"]])
    if report["orphan_entries"]:
        lines.extend(["", "### Orphan Ledger Entries"] + [f"- `{p}`" for p in report["orphan_entries"]])
    if report["stale_hashes"]:
        lines.extend(
            ["", "### Stale SHA-256 Digests"]
            + [f"- `{s['asset_path']}`" for s in report["stale_hashes"]]
        )
    return "\n".join(lines) + "\n"


def _classify_legacy_entry(raw_key: str, raw_entry: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    ai_data = raw_entry.get("ai_generation_data") if isinstance(raw_entry.get("ai_generation_data"), dict) else {}
    post_data = raw_entry.get("human_post_edit_data") if isinstance(raw_entry.get("human_post_edit_data"), dict) else {}

    tool = str(
        raw_entry.get("generation_tool")
        or ai_data.get("model_used")
        or "legacy-import"
    ).strip()
    prompt = str(raw_entry.get("prompt") or ai_data.get("prompt") or "").strip()
    tier = str(
        raw_entry.get("provenance_tier")
        or raw_entry.get("generation_type")
        or raw_entry.get("generation_pipeline")
        or ""
    ).strip().lower()
    raw_notes = str(raw_entry.get("notes") or "").strip()

    prompt_is_na = (
        not prompt
        or prompt.lower() in {"n/a", "none", "null", "unknown"}
        or prompt.lower().startswith("n/a ")
        or prompt.lower().startswith("n/a-")
        or prompt.lower().startswith("n/a:")
    )

    tool_lower = tool.lower()
    is_procedural = (
        tier == "procedural"
        or "procedural" in tool_lower
        or tool_lower.endswith(".py")
        or ".py)" in tool_lower
        or "svg" in tool_lower
        or "python" in tool_lower
        or "pillow" in tool_lower
        or "inkscape" in tool_lower
        or "lucide" in tool_lower
        or "canvas" in tool_lower
    )

    steps: list[dict[str, Any]] = []
    base_ref = raw_entry.get("base_human_sketch_ref")
    if isinstance(base_ref, str) and base_ref.strip():
        steps.append(
            {
                "actor": "human",
                "tool": "source-authoring",
                "summary": "Legacy human base source sketch/rig prior to compilation or generation.",
                "artifact_ref": base_ref.strip(),
            }
        )

    if is_procedural and (prompt_is_na or "procedural" in tool_lower or ".py" in tool_lower):
        steps.append(
            {
                "actor": "procedural",
                "tool": tool,
                "summary": prompt or f"Migrated procedural script generation using {tool}.",
            }
        )
    elif not prompt_is_na:
        if "+" in tool and "blender" not in tool_lower:
            parts = [p.strip() for p in tool.split("+") if p.strip()]
            ai_tool = parts[0]
            follow_tools = " + ".join(parts[1:]) if len(parts) > 1 else "manual-refinement"
            steps.append(
                {
                    "actor": "ai",
                    "tool": ai_tool,
                    "prompt": prompt,
                    "summary": "Legacy AI generation step migrated from v1 ledger.",
                }
            )
            steps.append(
                {
                    "actor": "human",
                    "tool": follow_tools,
                    "summary": "Legacy human/post-processing step migrated from v1 hybrid entry.",
                }
            )
        else:
            steps.append(
                {
                    "actor": "ai",
                    "tool": tool,
                    "prompt": prompt,
                    "summary": "Legacy AI generation step migrated from v1 ledger.",
                }
            )
            if tier in {"hybrid", "human_assisted", "human_edited", "sketch_to_ai_to_post_edit", "ai_to_post_edit"}:
                steps.append(
                    {
                        "actor": "human",
                        "tool": "manual-refinement",
                        "summary": str(
                            post_data.get("modification_description")
                            or "Legacy human refinement migrated from v1 hybrid provenance."
                        ),
                    }
                )
    else:
        actor = "procedural" if is_procedural else "human"
        summary_note = prompt if prompt.lower().startswith("n/a") and len(prompt) > 4 else None
        summary = summary_note or f"Migrated legacy {actor} asset created with {tool}."
        steps.append({"actor": actor, "tool": tool, "summary": summary})

    if post_data and not any(s["actor"] == "human" and s.get("summary") == post_data.get("modification_description") for s in steps):
        mod_desc = str(post_data.get("modification_description") or "").strip()
        if mod_desc and len(steps) < MAX_STEPS:
            post_step: dict[str, Any] = {
                "actor": "human",
                "tool": "post-processing",
                "summary": mod_desc,
            }
            if isinstance(post_data.get("source_file_ref"), str) and post_data["source_file_ref"].strip():
                post_step["artifact_ref"] = post_data["source_file_ref"].strip()
            steps.append(post_step)

    note_parts: list[str] = []
    if raw_notes:
        note_parts.append(raw_notes)
    if prompt_is_na and prompt and prompt.lower() not in {"n/a", "none"}:
        if prompt not in raw_notes:
            note_parts.append(f"Legacy prompt field: {prompt}")
    if "source" in raw_entry and raw_entry["source"]:
        note_parts.append(f"Legacy source: {raw_entry['source']}")
    if "workflow" in raw_entry and raw_entry["workflow"]:
        note_parts.append(f"Legacy workflow: {raw_entry['workflow']}")
    notes = " | ".join(note_parts) if note_parts else None
    return steps[:MAX_STEPS], notes


def migrate_legacy_ledger(
    repo_root: Path,
    *,
    apply: bool = True,
) -> dict[str, Any]:
    """Migrate a v1 docs/assets_ledger.json (or validate an already-v2 ledger) into schema v2."""
    resolved_root = repo_root.resolve()
    path = ledger_file_path(resolved_root)
    if not path.exists():
        data = {"version": SCHEMA_VERSION, "assets": {}}
        if apply:
            save_ledger(resolved_root, data)
        return {"migrated_count": 0, "already_v2": True, "assets": {}}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerCorruptError(f"Cannot migrate malformed JSON ledger {path}: {exc}") from exc

    if isinstance(raw, dict) and raw.get("version") == SCHEMA_VERSION:
        validate_ledger_data(raw)
        return {"migrated_count": 0, "already_v2": True, "assets": raw["assets"]}

    if isinstance(raw, list):
        items = [
            (str(item.get("asset_path") or item.get("file_name") or f"unknown_{i}"), item)
            for i, item in enumerate(raw)
            if isinstance(item, dict)
        ]
    elif isinstance(raw, dict):
        raw_assets = raw.get("assets", raw)
        if isinstance(raw_assets, list):
            items = [
                (str(item.get("asset_path") or item.get("file_name") or f"unknown_{i}"), item)
                for i, item in enumerate(raw_assets)
                if isinstance(item, dict)
            ]
        elif isinstance(raw_assets, dict):
            items = list(raw_assets.items())
        else:
            raise LedgerCorruptError("Legacy ledger 'assets' must be an object or list.")
    else:
        raise LedgerCorruptError(f"Unsupported legacy ledger root type: {type(raw).__name__}")

    migrated_assets: dict[str, dict[str, Any]] = {}
    now = utc_now_iso()
    for raw_key, raw_entry in items:
        if not isinstance(raw_entry, dict):
            raise LedgerCorruptError(f"Legacy entry {raw_key!r} must be an object.")

        if "steps" in raw_entry and isinstance(raw_entry["steps"], list):
            steps = _normalize_steps(raw_entry["steps"], resolved_root)
            notes = raw_entry.get("notes")
        else:
            steps, notes = _classify_legacy_entry(raw_key, raw_entry)

        candidate_path = raw_entry.get("asset_path") or raw_entry.get("file_name") or raw_key
        norm_path = normalize_asset_path(candidate_path, resolved_root)

        raw_status = str(raw_entry.get("status") or "active").lower()
        status = "deprecated" if "deprecated" in raw_status else "active"
        ts_logged = (
            raw_entry.get("logged_at")
            or raw_entry.get("timestamp_utc")
            or raw_entry.get("timestamp")
            or now
        )

        disk_file = resolved_root / norm_path
        entry: dict[str, Any] = {
            "asset_path": norm_path,
            "status": status,
            "ai_involvement": compute_ai_involvement(steps),
            "steps": steps,
            "logged_at": str(ts_logged),
            "updated_at": now,
        }
        if raw_entry.get("previous_paths"):
            entry["previous_paths"] = [
                normalize_asset_path(p, resolved_root)
                for p in raw_entry["previous_paths"]
            ]
        if notes:
            entry["notes"] = notes
        if disk_file.is_file():
            entry["sha256"] = compute_sha256(disk_file)
        elif raw_entry.get("sha256") and SHA256_RE.match(str(raw_entry["sha256"])):
            entry["sha256"] = str(raw_entry["sha256"])

        migrated_assets[norm_path] = entry

    result = {"version": SCHEMA_VERSION, "assets": migrated_assets}
    validate_ledger_data(result)
    if apply:
        save_ledger(resolved_root, result)
    return {
        "migrated_count": len(migrated_assets),
        "already_v2": False,
        "assets": migrated_assets,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crenet Asset Provenance Ledger CLI")
    parser.add_argument("--repo", default=".", help="Repository root path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_log = sub.add_parser("log", help="Log or update an asset provenance entry")
    p_log.add_argument("asset_path", help="Repository-relative asset path")
    p_log.add_argument("--steps-json", help="JSON array of 1..6 step objects")
    p_log.add_argument("--actor", choices=sorted(VALID_ACTORS), help="Single-step actor")
    p_log.add_argument("--tool", help="Single-step tool name")
    p_log.add_argument("--summary", help="Single-step concise description")
    p_log.add_argument("--prompt", help="Single-step prompt (required when actor=='ai')")
    p_log.add_argument("--artifact-ref", help="Single-step reference artifact path")
    p_log.add_argument("--seed", help="Single-step seed")
    p_log.add_argument("--license", dest="step_license", help="Single-step license identifier")
    p_log.add_argument("--status", default="active", choices=sorted(VALID_STATUSES))
    p_log.add_argument("--notes", help="Optional asset notes")
    p_log.add_argument("--append-steps", action="store_true")

    p_move = sub.add_parser("move", help="Graduate or relocate a tracked asset")
    p_move.add_argument("old_path")
    p_move.add_argument("new_path")
    p_move.add_argument("--actor", choices=sorted(VALID_ACTORS))
    p_move.add_argument("--tool")
    p_move.add_argument("--summary")
    p_move.add_argument("--prompt")
    p_move.add_argument("--notes")

    p_sync = sub.add_parser("sync-moves", help="Match deleted/renamed assets by SHA-256")
    p_sync.add_argument("--dry-run", action="store_true")

    p_verify = sub.add_parser("verify", help="Verify repo assets against docs/assets_ledger.json")
    p_verify.add_argument("--json", action="store_true", dest="as_json")
    p_verify.add_argument("--strict-hashes", action="store_true")
    p_verify.add_argument("--paths", nargs="*", help="Limit check to specific paths")

    p_report = sub.add_parser("report", help="Generate Publisher/M&A Asset Provenance Scorecard")
    p_report.add_argument("--json", action="store_true", dest="as_json")

    p_migrate = sub.add_parser("migrate", help="Migrate legacy v1 assets_ledger.json to v2")
    p_migrate.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo).resolve()

    try:
        if args.command == "log":
            if args.steps_json:
                steps = json.loads(args.steps_json)
            else:
                if not (args.actor and args.tool and args.summary):
                    raise ValueError(
                        "Provide either --steps-json OR (--actor, --tool, --summary)."
                    )
                step: dict[str, Any] = {
                    "actor": args.actor,
                    "tool": args.tool,
                    "summary": args.summary,
                }
                if args.prompt is not None:
                    step["prompt"] = args.prompt
                if args.artifact_ref is not None:
                    step["artifact_ref"] = args.artifact_ref
                if args.seed is not None:
                    step["seed"] = int(args.seed) if args.seed.isdigit() else args.seed
                if args.step_license is not None:
                    step["license"] = args.step_license
                steps = [step]

            entry = log_asset(
                repo_root,
                args.asset_path,
                steps,
                status=args.status,
                notes=args.notes,
                append_steps=args.append_steps,
            )
            print(json.dumps(entry, indent=2))
            return 0

        if args.command == "move":
            move_step: dict[str, Any] | None = None
            if args.actor or args.tool or args.summary:
                if not (args.actor and args.tool and args.summary):
                    raise ValueError(
                        "When providing a move step, pass --actor, --tool, and --summary."
                    )
                move_step = {
                    "actor": args.actor,
                    "tool": args.tool,
                    "summary": args.summary,
                }
                if args.prompt is not None:
                    move_step["prompt"] = args.prompt
            entry = move_asset(
                repo_root,
                args.old_path,
                args.new_path,
                step=move_step,
                notes=args.notes,
            )
            print(json.dumps(entry, indent=2))
            return 0

        if args.command == "sync-moves":
            res = sync_moves(repo_root, dry_run=args.dry_run)
            print(json.dumps(res, indent=2))
            return 0

        if args.command == "verify":
            res = verify_ledger(
                repo_root, paths=args.paths, strict_hashes=args.strict_hashes
            )
            if args.as_json:
                print(json.dumps(res, indent=2))
            else:
                if not res["enabled"]:
                    print("Asset provenance disabled via .crenet-governance.json.")
                elif res["ok"]:
                    print(
                        f"OK: {res['total_disk_assets']} active assets verified against {LEDGER_REL_PATH}."
                    )
                else:
                    if res["unlogged"]:
                        print(f"Unlogged assets ({len(res['unlogged'])}):")
                        for item in res["unlogged"]:
                            print(f"  - {item}")
                    if res["orphan_entries"]:
                        print(f"Orphan active entries ({len(res['orphan_entries'])}):")
                        for item in res["orphan_entries"]:
                            print(f"  - {item}")
                    if res["stale_hashes"]:
                        print(f"Stale SHA-256 hashes ({len(res['stale_hashes'])}):")
                        for item in res["stale_hashes"]:
                            print(f"  - {item['asset_path']}")
            return 0 if res["ok"] else 1

        if args.command == "report":
            report = generate_report(repo_root)
            if args.as_json:
                print(json.dumps(report, indent=2))
            else:
                print(format_report_markdown(report), end="")
            return 0

        if args.command == "migrate":
            res = migrate_legacy_ledger(repo_root, apply=not args.dry_run)
            print(
                json.dumps(
                    {
                        "already_v2": res["already_v2"],
                        "migrated_count": res["migrated_count"],
                    },
                    indent=2,
                )
            )
            return 0

    except (LedgerCorruptError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
