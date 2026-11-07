"""Unit tests for governance setup.sh installer script."""

import os
from pathlib import Path
import shutil
import subprocess
import pytest

GOVERNANCE_REPO_DIR = Path(__file__).resolve().parent.parent


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
    """Verifies setup.sh creates granular symlinks, .gitignore entries, and AGENTS.md references."""
    submodule_setup = mock_parent_workspace / "governance" / "setup.sh"

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

    # 3. Verify .gitignore
    gitignore = mock_parent_workspace / ".gitignore"
    assert gitignore.exists()
    gi_content = gitignore.read_text()
    assert ".agents/rules/governance" in gi_content
    assert ".agents/skills/mock-skill" in gi_content
    assert ".agents/skills/atomic-commit-chain" in gi_content

    # 4. Verify AGENTS.md includes marker and @[label](path) rule expansions
    agents_md = mock_parent_workspace / "AGENTS.md"
    assert agents_md.exists()
    agents_content = agents_md.read_text()
    assert "<!-- Crenet Governance Rules -->" in agents_content
    assert "Studio Governance Standards" in agents_content
    assert "- @[code-standards](.agents/rules/governance/code-standards.md)" in agents_content
    assert (
        "- @[environment-isolation](.agents/rules/governance/environment-isolation.md)"
        in agents_content
    )


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

    # Pre-populate .gitignore with a removed/old skill entry
    gitignore = mock_parent_workspace / ".gitignore"
    gitignore.write_text(
        "# Existing project ignores\nnode_modules/\n.agents/skills/old_removed_skill\n"
    )

    # Run setup.sh
    subprocess.run([str(submodule_setup)], cwd=mock_parent_workspace, check=True)

    gi_content = gitignore.read_text()
    # Old removed skill must be pruned
    assert ".agents/skills/old_removed_skill" not in gi_content
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

    # Verify .gitignore entries removed
    gi_content = (mock_parent_workspace / ".gitignore").read_text()
    assert ".agents/rules/governance" not in gi_content
    assert ".agents/skills/mock-skill" not in gi_content

    # Verify AGENTS.md governance reference removed
    agents_content = (mock_parent_workspace / "AGENTS.md").read_text()
    assert "Crenet Governance Rules" not in agents_content
    assert "Studio Governance Standards" not in agents_content
    assert ".agents/rules/governance" not in agents_content
