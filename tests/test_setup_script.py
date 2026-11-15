"""Unit tests for governance setup.sh installer script."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest

GOVERNANCE_REPO_DIR = Path(__file__).resolve().parent.parent

AI_IGNORE_SYMLINKS = (
    ".geminiignore",
    ".aiexclude",
    ".cursorignore",
    ".claudeignore",
    ".clineignore",
    ".rooignore",
    ".aiderignore",
    ".codeiumignore",
    ".rgignore",
)


@pytest.fixture
def mock_parent_workspace(tmp_path: Path) -> Path:
    """Fixture that simulates a parent game project containing governance as a submodule."""
    parent_dir = tmp_path / "mock_game_project"
    parent_dir.mkdir(parents=True, exist_ok=True)

    # Initialize a mock .git directory in parent
    (parent_dir / ".git").mkdir(exist_ok=True)

    # Copy governance repository essential contents into parent_dir/governance
    submodule_dir = parent_dir / "governance"
    submodule_dir.mkdir(parents=True, exist_ok=True)

    # Copy essential files/folders preserving symlinks
    shutil.copy(GOVERNANCE_REPO_DIR / "setup.sh", submodule_dir / "setup.sh")
    shutil.copy(GOVERNANCE_REPO_DIR / ".aiignore", submodule_dir / ".aiignore")
    shutil.copy(GOVERNANCE_REPO_DIR / "fastmcp.json", submodule_dir / "fastmcp.json")
    shutil.copy(GOVERNANCE_REPO_DIR / "pyproject.toml", submodule_dir / "pyproject.toml")
    shutil.copytree(
        GOVERNANCE_REPO_DIR / "mcp-server",
        submodule_dir / "mcp-server",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        GOVERNANCE_REPO_DIR / "schemas",
        submodule_dir / "schemas",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        GOVERNANCE_REPO_DIR / ".agents",
        submodule_dir / ".agents",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        GOVERNANCE_REPO_DIR / ".claude",
        submodule_dir / ".claude",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        GOVERNANCE_REPO_DIR / "scripts",
        submodule_dir / "scripts",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    # Create a mock skill to test setup.sh skill symlinking logic
    mock_skill_dir = submodule_dir / ".agents" / "skills" / "mock-skill"
    mock_skill_dir.mkdir(parents=True, exist_ok=True)
    (mock_skill_dir / "SKILL.md").write_text("Mock Skill")

    return parent_dir


def test_setup_script_initialization(mock_parent_workspace: Path):
    """Verifies setup.sh creates granular symlinks, .gitignore entries, and cleans legacy MCP links."""
    submodule_setup = mock_parent_workspace / "governance" / "setup.sh"

    # Pre-seed legacy per-repo MCP / plugin symlinks to verify migration cleanup
    legacy_mcp = mock_parent_workspace / ".mcp.json"
    legacy_mcp.symlink_to("governance/mcp.json")
    legacy_plugin_dir = mock_parent_workspace / ".agents" / "plugins"
    legacy_plugin_dir.mkdir(parents=True)
    legacy_plugin = legacy_plugin_dir / "crenet-governance"
    legacy_plugin.symlink_to("../../governance/.agents/plugins/crenet-governance")

    # Run setup.sh from parent project root
    res = subprocess.run(
        [str(submodule_setup)],
        cwd=mock_parent_workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Governance setup complete!" in res.stdout

    # 1. Verify rules symlink
    rules_link = mock_parent_workspace / ".agents" / "rules" / "governance"
    assert rules_link.is_symlink()
    assert rules_link.exists()

    # 2. Verify skills symlinks (mock-skill and atomic-commit-chain across .agents and .claude)
    mock_skill_link = mock_parent_workspace / ".agents" / "skills" / "mock-skill"
    assert mock_skill_link.is_symlink()
    assert mock_skill_link.exists()

    atomic_skill_link = mock_parent_workspace / ".agents" / "skills" / "atomic-commit-chain"
    assert atomic_skill_link.is_symlink()
    assert (atomic_skill_link / "SKILL.md").exists()
    assert (atomic_skill_link / "scripts" / "atomic_commit.py").exists()

    claude_skill_link = mock_parent_workspace / ".claude" / "skills" / "atomic-commit-chain"
    assert claude_skill_link.is_symlink()
    assert (claude_skill_link / "SKILL.md").exists()

    # 2b. Verify canonical .aiignore and agent ignore symlinks
    canonical_aiignore = mock_parent_workspace / ".aiignore"
    assert canonical_aiignore.is_file()
    for ignore_link_name in AI_IGNORE_SYMLINKS:
        ignore_link = mock_parent_workspace / ignore_link_name
        assert ignore_link.is_symlink()
        assert os.readlink(ignore_link) == ".aiignore"
        assert ignore_link.resolve() == canonical_aiignore.resolve()

    # 3. Verify .gitignore
    gitignore = mock_parent_workspace / ".gitignore"
    assert gitignore.exists()
    gi_content = gitignore.read_text()
    assert ".agents/rules/governance" in gi_content
    assert ".agents/skills/mock-skill" in gi_content
    assert ".agents/skills/atomic-commit-chain" in gi_content
    for ignore_link_name in AI_IGNORE_SYMLINKS:
        assert ignore_link_name in gi_content
    assert ".mcp.json" not in gi_content
    assert ".agents/plugins/crenet-governance" not in gi_content

    # 4. Verify AGENTS.md includes marker and @[label](path) rule expansions
    agents_md = mock_parent_workspace / "AGENTS.md"
    assert agents_md.exists()
    agents_content = agents_md.read_text()
    assert "<!-- Crenet Governance Rules -->" in agents_content
    assert "Studio Governance Standards" in agents_content
    assert "- @[code-standards](.agents/rules/governance/code-standards.md)" in agents_content
    assert (
        "- @[agent-execution-speed](.agents/rules/governance/agent-execution-speed.md)"
        in agents_content
    )
    assert "<!-- /Crenet Governance Rules -->" in agents_content

    # 5. Verify legacy per-repo MCP/plugin symlinks are absent/cleaned up
    assert not legacy_mcp.is_symlink() and not legacy_mcp.exists()
    assert not legacy_plugin.is_symlink() and not legacy_plugin.exists()


def test_setup_script_agents_md_idempotency_and_dynamic_rules(mock_parent_workspace: Path):
    """Verifies repeated setup.sh runs do not duplicate Studio Governance Standards and refresh rules."""
    submodule_setup = mock_parent_workspace / "governance" / "setup.sh"
    rules_dir = mock_parent_workspace / "governance" / ".agents" / "rules"

    # First run (creates AGENTS.md from scratch)
    subprocess.run([str(submodule_setup)], cwd=mock_parent_workspace, check=True)

    # Add a new rule file and run a second time
    (rules_dir / "custom-rule.md").write_text("# Custom Rule\n")
    subprocess.run([str(submodule_setup)], cwd=mock_parent_workspace, check=True)

    agents_content = (mock_parent_workspace / "AGENTS.md").read_text()
    assert agents_content.count("<!-- Crenet Governance Rules -->") == 1
    assert agents_content.count("## Studio Governance Standards") == 1
    assert "- @[custom-rule](.agents/rules/governance/custom-rule.md)" in agents_content

    # Remove the custom rule and run a third time to verify stale rule includes are pruned
    (rules_dir / "custom-rule.md").unlink()
    subprocess.run([str(submodule_setup)], cwd=mock_parent_workspace, check=True)

    agents_content_after = (mock_parent_workspace / "AGENTS.md").read_text()
    assert agents_content_after.count("## Studio Governance Standards") == 1
    assert "custom-rule" not in agents_content_after
    assert "- @[code-standards](.agents/rules/governance/code-standards.md)" in agents_content_after


def test_setup_script_prunes_removed_skills_from_gitignore(mock_parent_workspace: Path):
    """Verifies setup.sh prunes deleted/removed skill entries from .gitignore."""
    submodule_setup = mock_parent_workspace / "governance" / "setup.sh"

    # Pre-populate .gitignore with a removed/old skill and legacy .mcp.json entry
    gitignore = mock_parent_workspace / ".gitignore"
    gitignore.write_text(
        "# Existing project ignores\nnode_modules/\n.agents/skills/old_removed_skill\n.mcp.json\n"
    )

    # Run setup.sh
    subprocess.run([str(submodule_setup)], cwd=mock_parent_workspace, check=True)

    gi_content = gitignore.read_text()
    # Old removed skill and legacy .mcp.json must be pruned
    assert ".agents/skills/old_removed_skill" not in gi_content
    assert ".mcp.json" not in gi_content
    # Active skill must be present
    assert ".agents/skills/mock-skill" in gi_content
    assert "node_modules/" in gi_content


def test_setup_script_clean_teardown(mock_parent_workspace: Path):
    """Verifies setup.sh --clean removes symlinks, .gitignore entries, and AGENTS.md stubs."""
    submodule_setup = mock_parent_workspace / "governance" / "setup.sh"

    # Run setup.sh first
    subprocess.run([str(submodule_setup)], cwd=mock_parent_workspace, check=True)

    # Now run setup.sh --clean
    res = subprocess.run(
        [str(submodule_setup), "--clean"],
        cwd=mock_parent_workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Governance symlinks cleaned." in res.stdout

    # Verify symlinks are removed
    rules_link = mock_parent_workspace / ".agents" / "rules" / "governance"
    mock_skill_link = mock_parent_workspace / ".agents" / "skills" / "mock-skill"
    assert not rules_link.exists()
    assert not mock_skill_link.exists()
    assert (mock_parent_workspace / ".aiignore").is_file()
    for ignore_link_name in AI_IGNORE_SYMLINKS:
        ignore_link = mock_parent_workspace / ignore_link_name
        assert not ignore_link.is_symlink()
        assert not ignore_link.exists()

    # Verify .gitignore entries removed
    gi_content = (mock_parent_workspace / ".gitignore").read_text()
    assert ".agents/rules/governance" not in gi_content
    assert ".agents/skills/mock-skill" not in gi_content
    for ignore_link_name in AI_IGNORE_SYMLINKS:
        assert ignore_link_name not in gi_content

    # Verify AGENTS.md governance reference removed
    agents_content = (mock_parent_workspace / "AGENTS.md").read_text()
    assert "Crenet Governance Rules" not in agents_content
    assert "Studio Governance Standards" not in agents_content
    assert ".agents/rules/governance" not in agents_content


def test_setup_script_preserves_custom_claude_files_and_supports_opt_out(mock_parent_workspace: Path):
    """Verifies setup.sh never clobbers custom .claude/ files and supports --no-asset-provenance."""
    submodule_setup = mock_parent_workspace / "governance" / "setup.sh"
    custom_cmd = mock_parent_workspace / ".claude" / "commands" / "release.md"
    custom_cmd.parent.mkdir(parents=True)
    custom_cmd.write_text("# Custom Release Command\n", encoding="utf-8")
    custom_settings = mock_parent_workspace / ".claude" / "settings.json"
    custom_settings.write_text('{"custom": true}\n', encoding="utf-8")

    subprocess.run(
        [str(submodule_setup), "--no-asset-provenance"],
        cwd=mock_parent_workspace,
        check=True,
    )

    assert custom_cmd.read_text(encoding="utf-8") == "# Custom Release Command\n"
    assert json.loads(custom_settings.read_text(encoding="utf-8")) == {"custom": True}
    assert (mock_parent_workspace / ".claude" / "hooks" / "check_assets.sh").is_symlink()
    cfg = json.loads((mock_parent_workspace / ".crenet-governance.json").read_text(encoding="utf-8"))
    assert cfg["asset_provenance"] is False

    # Teardown leaves custom .claude files untouched
    subprocess.run([str(submodule_setup), "--clean"], cwd=mock_parent_workspace, check=True)
    assert custom_cmd.exists()
    assert custom_settings.exists()


def test_setup_script_global_install_and_clean(mock_parent_workspace: Path, tmp_path: Path):
    """Verifies setup.sh --global merges crenet-governance into $HOME configs without overwriting keys."""
    submodule_setup = mock_parent_workspace / "governance" / "setup.sh"
    fake_home = tmp_path / "fake_home"

    agy_cfg_path = fake_home / ".gemini" / "config" / "mcp_config.json"
    agy_cfg_path.parent.mkdir(parents=True)
    agy_cfg_path.write_text(json.dumps({"mcpServers": {"existing-agy": {"command": "foo"}}}), encoding="utf-8")

    claude_cfg_path = fake_home / ".claude.json"
    claude_cfg_path.write_text(
        json.dumps({"numStartups": 3, "mcpServers": {"existing-claude": {"command": "bar"}}}),
        encoding="utf-8",
    )

    env = {**os.environ, "HOME": str(fake_home), "CRENET_SKIP_UV_SYNC": "1"}
    env.pop("XDG_DATA_HOME", None)
    env.pop("CRENET_GOVERNANCE_HOME", None)

    subprocess.run([str(submodule_setup), "--global"], cwd=tmp_path, env=env, check=True)

    global_dir = fake_home / ".local" / "share" / "crenet-governance"
    launcher = global_dir / "scripts" / "run-mcp.sh"
    assert launcher.exists()
    assert os.access(launcher, os.X_OK)
    assert (global_dir / "mcp-server" / "server.py").exists()

    expected_launcher = str(launcher)
    agy_cfg = json.loads(agy_cfg_path.read_text(encoding="utf-8"))
    assert agy_cfg["mcpServers"]["existing-agy"] == {"command": "foo"}
    assert agy_cfg["mcpServers"]["crenet-governance"] == {"command": expected_launcher, "args": []}

    cursor_cfg = json.loads((fake_home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert cursor_cfg["mcpServers"]["crenet-governance"] == {"command": expected_launcher, "args": []}

    claude_cfg = json.loads(claude_cfg_path.read_text(encoding="utf-8"))
    assert claude_cfg["numStartups"] == 3
    assert claude_cfg["mcpServers"]["existing-claude"] == {"command": "bar"}
    assert claude_cfg["mcpServers"]["crenet-governance"] == {
        "type": "stdio",
        "command": expected_launcher,
        "args": [],
    }

    subprocess.run([str(submodule_setup), "--global", "--clean"], cwd=tmp_path, env=env, check=True)
    assert not global_dir.exists()

    agy_after = json.loads(agy_cfg_path.read_text(encoding="utf-8"))
    assert "crenet-governance" not in agy_after["mcpServers"]
    assert agy_after["mcpServers"]["existing-agy"] == {"command": "foo"}

    cursor_after = json.loads((fake_home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert "crenet-governance" not in cursor_after["mcpServers"]

    claude_after = json.loads(claude_cfg_path.read_text(encoding="utf-8"))
    assert claude_after["numStartups"] == 3
    assert "crenet-governance" not in claude_after["mcpServers"]
    assert claude_after["mcpServers"]["existing-claude"] == {"command": "bar"}


def test_setup_script_rejects_non_git_parent(tmp_path: Path):
    """Verifies per-repo setup.sh refuses to mutate a non-Git parent directory."""
    parent_dir = tmp_path / "non_git_dir"
    gov_dir = parent_dir / "governance"
    gov_dir.mkdir(parents=True)
    shutil.copy(GOVERNANCE_REPO_DIR / "setup.sh", gov_dir / "setup.sh")

    res = subprocess.run(
        [str(gov_dir / "setup.sh")],
        cwd=parent_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "not a Git repository" in res.stderr
    assert not (parent_dir / "AGENTS.md").exists()
    assert not (parent_dir / ".agents").exists()
