#!/usr/bin/env python3
"""Crenet Governance asset provenance hook handler for Antigravity, Cursor, and Claude Code.

Generic / Antigravity / Cursor flow (`.agents/hooks.json`, `.cursor/hooks.json`):
  - `post_tool_use` (alias `agy_post_tool_use`): Tool-filtered
    (`invoke_subagent|send_message|run_command|call_mcp_tool|mcp_.*`).
    Fast-skips benign read/test `run_command` calls in <1ms; otherwise audits
    assets via `ledger.verify_ledger()` and records unlogged assets + captured
    intermediate prompt hints into a per-conversation marker in `/tmp`.
  - `post_invocation` (alias `agy_post_invocation`): Zero-git-scan marker consumer.
    Consumes the `/tmp` marker (if present) and returns `injectSteps` with a
    `systemMessage` prompt while keeping normal coding turns near 0ms overhead.
  - `stop` (alias `agy_stop`): Session safety net respecting
    `continuationActive`, `stop_hook_active`, and `terminationReason` loop guards.
  - Payload normalization supports Antigravity (`workspacePaths`, `conversationId`),
    Cursor (`workspace_roots`, `conversation_id`), and `cwd` / `session_id`.

Claude Code flow (`.claude/settings.json`):
  - `claude_post_tool_use`: Tool-filtered immediate feedback (`decision: block`).
  - `claude_stop` / `claude`: Stop hook respecting `stop_hook_active`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ledger import LedgerCorruptError, is_asset_provenance_enabled, verify_ledger  # noqa: E402

MARKER_DIR = Path("/tmp")
MARKER_PREFIX = "crenet-governance-pending-"

SAFE_COMMAND_RE = re.compile(
    r"^(?:"
    r"git\s+(?:status|diff|log|show|grep|branch|rev-parse|ls-files|remote|config)\b"
    r"|ls\b|pwd\b|which\b|command\s+-v\b|head\b|tail\b|cat\b|wc\b"
    r"|rg\b|grep\b|find\b"
    r"|(?:uv\s+run\s+|python3?\s+-m\s+)?pytest\b"
    r"|make\s+(?:test|typecheck|lint|check|lint-skills|test-all)\b"
    r"|npm\s+(?:test|run\s+(?:test|typecheck|lint|check))\b"
    r"|npx\s+(?:vitest|tsc)\b"
    r")"
)
SHELL_CHAIN_CHARS = ("&&", "||", ";", "|", ">", ">>", "`", "$(", "\n")


def find_repo_root(start_dir: Path | None = None) -> Path:
    """Resolve the repository root (containing .git or docs/ or fallback to cwd)."""
    cur = (start_dir or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists() or (candidate / "docs" / "assets_ledger.json").exists():
            return candidate
    return cur


def _read_stdin_payload() -> dict[str, Any]:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read().strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _resolve_workspace_root(payload: dict[str, Any]) -> Path:
    for key in ("workspacePaths", "workspace_paths", "workspace_roots"):
        paths = payload.get(key)
        if isinstance(paths, list) and paths and isinstance(paths[0], str):
            p = Path(paths[0])
            if p.exists():
                return find_repo_root(p)
    cwd_val = payload.get("cwd")
    if isinstance(cwd_val, str) and cwd_val:
        p = Path(cwd_val)
        if p.exists():
            return find_repo_root(p)
    return find_repo_root()


def _marker_path(payload: dict[str, Any]) -> Path:
    conv_id = str(
        payload.get("conversationId")
        or payload.get("conversation_id")
        or payload.get("session_id")
        or "default"
    )
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", conv_id)[:80] or "default"
    return MARKER_DIR / f"{MARKER_PREFIX}{safe_id}.json"


def _clear_marker(marker: Path) -> None:
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


def is_benign_shell_command(command: str) -> bool:
    """Return True if a single shell command is clearly read-only / test / lint."""
    stripped = command.strip()
    if not stripped:
        return True
    if any(token in stripped for token in SHELL_CHAIN_CHARS):
        return False
    return bool(SAFE_COMMAND_RE.match(stripped))


def extract_tool_info(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract normalized (tool_name, args_dict) from Antigravity or Claude payloads."""
    tool_call = payload.get("toolCall") or payload.get("tool_call")
    if isinstance(tool_call, dict):
        name = str(tool_call.get("name") or tool_call.get("toolName") or "")
        args = tool_call.get("args") or tool_call.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw": args}
        return name, args if isinstance(args, dict) else {}

    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    args = payload.get("tool_input") or payload.get("args") or {}
    return name, args if isinstance(args, dict) else {}


def should_fast_skip(tool_name: str, tool_args: dict[str, Any]) -> bool:
    """Skip expensive git/asset scans for benign shell test/read commands."""
    if tool_name in {"run_command", "Bash"}:
        cmd = str(
            tool_args.get("CommandLine")
            or tool_args.get("command")
            or tool_args.get("cmd")
            or ""
        )
        return is_benign_shell_command(cmd)
    return False


def extract_prompt_hint(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """Capture any prompt, subagent brief, or media-generation command snippet."""
    nested = tool_args.get("Arguments")
    search_dicts = [tool_args]
    if isinstance(nested, dict):
        search_dicts.append(nested)

    for mapping in search_dicts:
        for key in ("prompt", "Prompt", "brief", "Message", "description", "query"):
            val = mapping.get(key)
            if isinstance(val, str) and val.strip():
                snippet = val.strip().replace("\n", " ")
                if len(snippet) > 280:
                    snippet = snippet[:277] + "..."
                return f"{tool_name}.{key}: {snippet}"

    if tool_name in {"run_command", "Bash"}:
        cmd = str(tool_args.get("CommandLine") or tool_args.get("command") or "").strip()
        if cmd:
            if len(cmd) > 200:
                cmd = cmd[:197] + "..."
            return f"{tool_name}: {cmd}"
    return None


def format_violation_message(
    verify: dict[str, Any],
    *,
    prompt_hint: str | None = None,
) -> str:
    """Build concise, actionable guidance referencing MCP tools and graduation moves."""
    parts: list[str] = ["[ASSET PROVENANCE AUDIT REQUIRED]"]

    unlogged = verify.get("unlogged", [])
    orphans = verify.get("orphan_entries", [])
    candidates = verify.get("graduation_candidates", [])

    if unlogged:
        preview = ", ".join(f"`{p}`" for p in unlogged[:8])
        extra = f" (+{len(unlogged) - 8} more)" if len(unlogged) > 8 else ""
        parts.append(f"Unlogged asset files on disk: {preview}{extra}.")

    if orphans:
        preview = ", ".join(f"`{p}`" for p in orphans[:8])
        extra = f" (+{len(orphans) - 8} more)" if len(orphans) > 8 else ""
        parts.append(f"Orphan active ledger entries (missing on disk): {preview}{extra}.")

    if candidates:
        cand_strs = []
        for c in candidates[:4]:
            matches = ", ".join(c["matching_ledger_paths"])
            cand_strs.append(f"`{matches}` -> `{c['unlogged_path']}`")
        parts.append(
            "Matching SHA-256 graduation/move candidates detected: "
            + "; ".join(cand_strs)
            + ". Call `move_asset_provenance` (or `scripts/ledger.py move`) to preserve earlier steps."
        )

    if prompt_hint:
        parts.append(
            f"Captured intermediate tool context (preserve verbatim in AI `prompt` steps if applicable): `{prompt_hint}`"
        )

    parts.append(
        "Resolve via `log_asset_provenance` / `move_asset_provenance` MCP tools (or `scripts/ledger.py log|move`) in `docs/assets_ledger.json`."
    )
    return " ".join(parts)


def handle_agy_post_tool_use(payload: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    tool_name, tool_args = extract_tool_info(payload)
    if should_fast_skip(tool_name, tool_args):
        return {}
    try:
        if not is_asset_provenance_enabled(repo_root):
            return {}
        verify = verify_ledger(repo_root)
    except LedgerCorruptError as exc:
        marker = _marker_path(payload)
        marker.write_text(
            json.dumps({"corrupt_error": str(exc)}), encoding="utf-8"
        )
        return {}

    if verify["ok"]:
        return {}

    marker = _marker_path(payload)
    prior_hint = None
    if marker.exists():
        try:
            prior = json.loads(marker.read_text(encoding="utf-8"))
            prior_hint = prior.get("prompt_hint")
        except (OSError, json.JSONDecodeError):
            prior_hint = None

    hint = extract_prompt_hint(tool_name, tool_args) or prior_hint
    marker_data = {
        "verify": verify,
        "prompt_hint": hint,
    }
    marker.write_text(json.dumps(marker_data), encoding="utf-8")
    return {}


def handle_agy_post_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    marker = _marker_path(payload)
    if not marker.exists():
        return {}
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _clear_marker(marker)
        return {}
    _clear_marker(marker)

    if "corrupt_error" in data:
        msg = (
            f"[ASSET PROVENANCE ERROR] docs/assets_ledger.json is invalid: {data['corrupt_error']}. "
            "Run `scripts/ledger.py migrate` or fix the JSON schema immediately."
        )
    else:
        verify = data.get("verify", {})
        if verify.get("ok", True):
            return {}
        msg = format_violation_message(verify, prompt_hint=data.get("prompt_hint"))

    return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}


def handle_agy_stop(payload: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    _clear_marker(_marker_path(payload))
    if payload.get("continuationActive") or payload.get("stop_hook_active"):
        return {"decision": "allow"}
    reason = payload.get("terminationReason")
    if reason is not None and reason != "NO_TOOL_CALL":
        return {"decision": "allow"}

    try:
        if not is_asset_provenance_enabled(repo_root):
            return {"decision": "allow"}
        verify = verify_ledger(repo_root)
    except LedgerCorruptError as exc:
        return {
            "decision": "block",
            "reason": (
                f"Asset provenance ledger validation failed: {exc}. "
                "Run `scripts/ledger.py migrate` or repair `docs/assets_ledger.json`."
            ),
        }

    if verify["ok"]:
        return {"decision": "allow"}
    return {"decision": "block", "reason": format_violation_message(verify)}


def handle_claude_post_tool_use(payload: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    tool_name, tool_args = extract_tool_info(payload)
    if should_fast_skip(tool_name, tool_args):
        return {}
    try:
        if not is_asset_provenance_enabled(repo_root):
            return {}
        verify = verify_ledger(repo_root)
    except LedgerCorruptError as exc:
        return {
            "decision": "block",
            "reason": f"Asset provenance ledger validation failed: {exc}",
        }
    if verify["ok"]:
        return {}
    hint = extract_prompt_hint(tool_name, tool_args)
    return {
        "decision": "block",
        "reason": format_violation_message(verify, prompt_hint=hint),
    }


def handle_claude_stop(payload: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    if payload.get("stop_hook_active") or payload.get("continuationActive"):
        return {}
    try:
        if not is_asset_provenance_enabled(repo_root):
            return {}
        verify = verify_ledger(repo_root)
    except LedgerCorruptError as exc:
        return {
            "decision": "block",
            "reason": f"Asset provenance ledger validation failed: {exc}",
        }
    if verify["ok"]:
        return {}
    return {"decision": "block", "reason": format_violation_message(verify)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crenet Asset Provenance Hook Runner")
    parser.add_argument(
        "--mode",
        choices=[
            "post_tool_use",
            "post_invocation",
            "stop",
            "agy_post_tool_use",
            "agy_post_invocation",
            "agy_stop",
            "claude_post_tool_use",
            "claude_stop",
            "claude",
        ],
        default="stop",
    )
    args = parser.parse_args(argv)
    payload = _read_stdin_payload()
    repo_root = _resolve_workspace_root(payload)

    if args.mode in {"agy_post_tool_use", "post_tool_use"}:
        result = handle_agy_post_tool_use(payload, repo_root)
    elif args.mode in {"agy_post_invocation", "post_invocation"}:
        result = handle_agy_post_invocation(payload)
    elif args.mode in {"agy_stop", "stop"}:
        result = handle_agy_stop(payload, repo_root)
    elif args.mode == "claude_post_tool_use":
        result = handle_claude_post_tool_use(payload, repo_root)
    else:
        result = handle_claude_stop(payload, repo_root)

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
