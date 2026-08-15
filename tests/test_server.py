"""Unit tests for Crenet Core Standards MCP Governance Server."""

json = __import__("json")
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch
import pytest

from server import log_ai_asset, verify_environment_isolation


def call_fn(tool, *args, **kwargs):
    """Helper to invoke underlying tool function in FastMCP."""
    fn = getattr(tool, "fn", tool)
    return fn(*args, **kwargs)


def test_log_ai_asset_creates_workspace_docs_ledger_hybrid_pipeline(tmp_path: Path):
    """Verifies log_ai_asset records full hybrid pipeline metadata in ./docs/assets_ledger.json."""
    with patch("os.getcwd", return_value=str(tmp_path)):
        result = call_fn(
            log_ai_asset,
            file_name="caelum_texture.png",
            generation_type="sketch_to_ai_to_post_edit",
            base_human_sketch_ref="sketches/caelum_01.png",
            ai_prompt_used="hyperrealistic game texture armor detail",
            ai_model_used="Stable Diffusion XL",
            ai_seed="424242",
            human_source_file_ref="art/caelum_texture.psd",
            human_modification_description="repainted cockpit highlights and adjusted silhouette",
        )

        expected_ledger = tmp_path / "docs" / "assets_ledger.json"
        assert expected_ledger.exists()
        assert "Successfully logged AI asset" in result
        assert "sketch_to_ai_to_post_edit" in result

        with open(expected_ledger, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert len(data) == 1
        entry = data[0]
        assert entry["file_name"] == "caelum_texture.png"
        assert entry["generation_type"] == "sketch_to_ai_to_post_edit"
        assert entry["base_human_sketch_ref"] == "sketches/caelum_01.png"
        assert entry["ai_generation_data"]["prompt"] == "hyperrealistic game texture armor detail"
        assert entry["ai_generation_data"]["model_used"] == "Stable Diffusion XL"
        assert entry["ai_generation_data"]["seed"] == "424242"
        assert entry["human_post_edit_data"]["source_file_ref"] == "art/caelum_texture.psd"
        assert (
            entry["human_post_edit_data"]["modification_description"]
            == "repainted cockpit highlights and adjusted silhouette"
        )
        assert "timestamp_utc" in entry


def test_log_ai_asset_invalid_generation_type():
    """Verifies ValueError is raised for unrecognized generation_type."""
    with pytest.raises(ValueError) as exc_info:
        call_fn(log_ai_asset, file_name="asset.png", generation_type="invalid_type")
    assert "Invalid generation_type 'invalid_type'" in str(exc_info.value)


@pytest.mark.parametrize(
    "cmd_mock_responses, expected_docs_dir_name",
    [
        (
            {("git", "rev-parse", "--show-superproject-working-tree"): "{tmp_path}/superproject\n"},
            "superproject/docs",
        ),
        (
            {
                ("git", "rev-parse", "--show-superproject-working-tree"): "\n",
                ("git", "rev-parse", "--show-toplevel"): "{tmp_path}/git_root\n",
            },
            "git_root/docs",
        ),
        (
            Exception("git not found"),
            "docs",
        ),
    ],
    ids=["superproject", "toplevel_fallback", "getcwd_fallback"],
)
def test_log_ai_asset_workspace_resolution(
    tmp_path: Path, cmd_mock_responses, expected_docs_dir_name
):
    """Verifies that the ledger is written to the correctly resolved workspace."""

    def mock_subprocess_run(cmd, *args, **kwargs):
        if isinstance(cmd_mock_responses, Exception):
            raise cmd_mock_responses

        cmd_tuple = tuple(cmd)
        if cmd_tuple in cmd_mock_responses:
            return MagicMock(
                stdout=cmd_mock_responses[cmd_tuple].format(tmp_path=str(tmp_path)), returncode=0
            )
        return MagicMock(stdout="", returncode=0)

    with patch("os.getcwd", return_value=str(tmp_path)), patch(
        "subprocess.run", side_effect=mock_subprocess_run
    ):
        call_fn(log_ai_asset, file_name="asset.png", generation_type="pure_ai")
        
        expected_docs_path = tmp_path / expected_docs_dir_name
        assert (expected_docs_path / "assets_ledger.json").exists()


@pytest.mark.parametrize(
    "compliant_email",
    [
        "developer@crenet.games",
        "artist@crenet.games",
    ],
)
def test_verify_environment_isolation_compliant_emails(compliant_email: str):
    """Verifies that allowed Crenet emails pass the environment isolation check."""

    def mock_subprocess_run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "config", "user.email"]:
            return MagicMock(stdout=f"{compliant_email}\n", returncode=0)
        if cmd[:2] == ["ps", "aux"]:
            return MagicMock(stdout="root 1 0.0 bash\n", returncode=0)
        return MagicMock(stdout="", returncode=0)

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        result = call_fn(verify_environment_isolation)
        assert "Environment Isolation Check Passed" in result
        assert compliant_email in result


def test_verify_environment_isolation_blocks_corporate_email():
    """Verifies that non-compliant git emails raise RuntimeError."""

    def mock_subprocess_run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "config", "user.email"]:
            return MagicMock(stdout="employee@bigcorp-consulting.com\n", returncode=0)
        return MagicMock(stdout="", returncode=0)

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        with pytest.raises(RuntimeError) as exc_info:
            call_fn(verify_environment_isolation)
        assert "Environment Isolation Violation: Git email" in str(exc_info.value)
        assert "employee@bigcorp-consulting.com" in str(exc_info.value)


def test_verify_environment_isolation_blocks_active_network_profile():
    """Verifies that detected unauthorized network processes raise RuntimeError."""

    def mock_subprocess_run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "config", "user.email"]:
            return MagicMock(stdout="developer@crenet.games\n", returncode=0)
        if cmd[:2] == ["ps", "aux"]:
            return MagicMock(
                stdout="user 1234 1.0 /opt/cisco/anyconnect/bin/vpnagentd\n", returncode=0
            )
        return MagicMock(stdout="", returncode=0)

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        with pytest.raises(RuntimeError) as exc_info:
            call_fn(verify_environment_isolation)
        assert "unauthorized network profile" in str(exc_info.value).lower()
