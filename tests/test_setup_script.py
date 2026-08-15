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

    # 2. Verify skills symlink
    mock_skill_link = mock_parent_workspace / ".agents" / "skills" / "mock-skill"
    assert mock_skill_link.is_symlink()
    assert mock_skill_link.exists()

    # 3. Verify .gitignore
    gitignore = mock_parent_workspace / ".gitignore"
    assert gitignore.exists()
    gi_content = gitignore.read_text()
    assert ".agents/rules/governance" in gi_content
    assert ".agents/skills/mock-skill" in gi_content

    # 4. Verify AGENTS.md
    agents_md = mock_parent_workspace / "AGENTS.md"
    assert agents_md.exists()
    assert "Studio Governance Standards" in agents_md.read_text()


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
    assert "Studio Governance Standards" not in agents_content
