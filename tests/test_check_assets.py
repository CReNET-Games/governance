"""Unit tests for scripts/check_assets.py across Antigravity and Claude Code hooks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

import check_assets  # noqa: E402
import ledger  # noqa: E402


@pytest.fixture
def repo_with_git(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@crenet.games"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.parametrize(
    ("command", "expected_benign"),
    [
        ("git status", True),
        ("git diff --stat", True),
        ("pytest -m 'not llm_eval' -v", True),
        ("uv run pytest -v", True),
        ("make test", True),
        ("rg handle_agy_stop scripts/", True),
        ("python3 generate_art.py", False),
        ("cp a.png b.png", False),
        ("git status && cp a.png b.png", False),
        ("ls > out.png", False),
    ],
)
def test_benign_shell_command_fast_skip(command: str, expected_benign: bool):
    assert check_assets.is_benign_shell_command(command) is expected_benign


def test_agy_post_tool_use_fast_skips_benign_command(repo_with_git: Path):
    (repo_with_git / "unlogged.png").write_bytes(b"png")
    payload = {
        "conversationId": "conv-fast-skip",
        "workspacePaths": [str(repo_with_git)],
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
    }
    marker = check_assets._marker_path(payload)
    marker.unlink(missing_ok=True)
    out = check_assets.handle_agy_post_tool_use(payload, repo_with_git)
    assert out == {}
    assert not marker.exists()


def test_agy_two_stage_captures_intermediate_prompt_and_clears_marker(repo_with_git: Path):
    (repo_with_git / "artifacts/draft.png").parent.mkdir(parents=True)
    (repo_with_git / "artifacts/draft.png").write_bytes(b"img")

    payload = {
        "conversationId": "conv-intermediate-prompt",
        "workspacePaths": [str(repo_with_git)],
        "toolCall": {
            "name": "invoke_subagent",
            "args": {
                "TypeName": "image-generator",
                "Prompt": "Dark biomechanical station corridor with cyan terminal glow",
            },
        },
    }
    marker = check_assets._marker_path(payload)
    marker.unlink(missing_ok=True)

    post_tool_res = check_assets.handle_agy_post_tool_use(payload, repo_with_git)
    assert post_tool_res == {}
    assert marker.exists()

    post_inv_res = check_assets.handle_agy_post_invocation(payload)
    assert not marker.exists()
    injected = post_inv_res["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "artifacts/draft.png" in injected
    assert "Dark biomechanical station corridor" in injected
    assert "log_asset_provenance" in injected

    # Second PostInvocation without new PostToolUse marker is a zero-scan no-op
    assert check_assets.handle_agy_post_invocation(payload) == {}


@pytest.mark.parametrize(
    ("extra_payload", "expected_decision"),
    [
        ({"terminationReason": "NO_TOOL_CALL"}, "block"),
        ({}, "block"),
        ({"continuationActive": True, "terminationReason": "NO_TOOL_CALL"}, "allow"),
        ({"stop_hook_active": True}, "allow"),
        ({"terminationReason": "ERROR"}, "allow"),
        ({"terminationReason": "MAX_TURNS"}, "allow"),
        ({"terminationReason": "USER_CANCELED"}, "allow"),
    ],
)
def test_agy_stop_loop_guards(repo_with_git: Path, extra_payload: dict, expected_decision: str):
    (repo_with_git / "new.png").write_bytes(b"png")
    payload = {"conversationId": "conv-stop", **extra_payload}
    res = check_assets.handle_agy_stop(payload, repo_with_git)
    assert res["decision"] == expected_decision


def test_stop_blocks_on_corrupt_ledger_and_respects_opt_out(repo_with_git: Path):
    (repo_with_git / "docs").mkdir()
    (repo_with_git / "docs/assets_ledger.json").write_text('{"assets": {}}', encoding="utf-8")
    res = check_assets.handle_agy_stop({"conversationId": "conv-corrupt"}, repo_with_git)
    assert res["decision"] == "block"
    assert "migrate" in res["reason"].lower()

    (repo_with_git / ".crenet-governance.json").write_text('{"asset_provenance": false}', encoding="utf-8")
    assert check_assets.handle_agy_stop({"conversationId": "conv-corrupt"}, repo_with_git) == {"decision": "allow"}


def test_claude_hooks_respect_stop_guard_and_log_pass(repo_with_git: Path):
    img = repo_with_git / "hero.png"
    img.write_bytes(b"img")

    block_post = check_assets.handle_claude_post_tool_use(
        {"tool_name": "mcp__gemini__generate", "tool_input": {"prompt": "Exo suit"}},
        repo_with_git,
    )
    assert block_post["decision"] == "block"
    assert "Exo suit" in block_post["reason"]

    # Stop guard prevents infinite recursion
    assert check_assets.handle_claude_stop({"stop_hook_active": True}, repo_with_git) == {}
    assert check_assets.handle_claude_stop({}, repo_with_git)["decision"] == "block"

    ledger.log_asset(
        repo_with_git,
        "hero.png",
        [{"actor": "ai", "tool": "gemini", "prompt": "Exo suit", "summary": "Generated suit."}],
    )
    assert check_assets.handle_claude_stop({}, repo_with_git) == {}


def test_agent_agnostic_modes_and_cursor_workspace_roots(
    repo_with_git: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import io
    import json
    import sys

    img = repo_with_git / "cursor_sprite.png"
    img.write_bytes(b"img")

    payload = {
        "conversation_id": "cursor-conv-1",
        "workspace_roots": [str(repo_with_git)],
        "tool_name": "mcp_image_gen",
        "tool_input": {"prompt": "Pixel shield icon"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert check_assets.main(["--mode", "post_tool_use"]) == 0
    assert json.loads(capsys.readouterr().out) == {}

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"conversation_id": "cursor-conv-1"})))
    assert check_assets.main(["--mode", "post_invocation"]) == 0
    inv_out = json.loads(capsys.readouterr().out)
    assert "cursor_sprite.png" in inv_out["injectSteps"][0]["systemMessage"]["systemMessage"]

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"conversation_id": "cursor-conv-1", "workspace_roots": [str(repo_with_git)]})),
    )
    assert check_assets.main(["--mode", "stop"]) == 0
    stop_out = json.loads(capsys.readouterr().out)
    assert stop_out["decision"] == "block"
    assert "cursor_sprite.png" in stop_out["reason"]
