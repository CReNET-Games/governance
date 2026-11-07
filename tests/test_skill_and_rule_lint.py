"""Deterministic static linter for Crenet Governance skills, rules, and documentation."""

from pathlib import Path
import re
import subprocess
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".agents"
RULES_DIR = AGENTS_DIR / "rules"
SKILLS_DIR = AGENTS_DIR / "skills"
CLAUDE_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

# Token budget ceilings (estimated at ~4 chars per token)
MAX_SKILL_TOKENS = 2500
MAX_RULE_TOKENS = 800

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
FENCED_CODE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
MD_LINK_RE = re.compile(r"@?\[[^\]]+\]\(([^)]+)\)")


def parse_simple_frontmatter(markdown_text: str) -> dict[str, str] | None:
    """Parse YAML frontmatter (supporting single-line and folded block scalars) without third-party deps."""
    match = FRONTMATTER_RE.match(markdown_text)
    if not match:
        return None

    raw = match.group(1)
    result: dict[str, str] = {}
    current_key: str | None = None
    buffer: list[str] = []

    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and ":" in line:
            if current_key is not None:
                result[current_key] = " ".join(buffer).strip()
            key, val = line.split(":", 1)
            current_key = key.strip()
            val = val.strip()
            if val in (">-", ">", "|", "|-", ""):
                buffer = []
            else:
                buffer = [val.strip("'\"")]
        elif current_key is not None:
            buffer.append(line.strip())

    if current_key is not None:
        result[current_key] = " ".join(buffer).strip()

    return result


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~4 characters per token for English/Markdown)."""
    return max(1, len(text) // 4)


def get_skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def get_rule_files() -> list[Path]:
    return sorted(RULES_DIR.glob("*.md"))


def get_markdown_docs() -> list[Path]:
    docs = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "TODO.md",
        AGENTS_DIR / "AGENTS.md",
        *get_rule_files(),
        *get_skill_files(),
    ]
    return [d for d in docs if d.exists()]


@pytest.mark.parametrize("skill_md", get_skill_files(), ids=lambda p: p.parent.name)
def test_skill_frontmatter_and_token_budget(skill_md: Path):
    """Every SKILL.md must have valid frontmatter (name, trigger description) and fit the token budget."""
    content = skill_md.read_text(encoding="utf-8")
    fm = parse_simple_frontmatter(content)
    assert fm is not None, f"{skill_md.relative_to(REPO_ROOT)} is missing YAML frontmatter (--- ... ---)"

    expected_name = skill_md.parent.name
    assert fm.get("name") == expected_name, (
        f"Frontmatter 'name' ({fm.get('name')!r}) must match skill directory name ({expected_name!r})"
    )

    description = fm.get("description", "")
    assert len(description) >= 40, (
        f"Skill '{expected_name}' description is too short ({len(description)} chars); "
        "include clear trigger conditions."
    )

    tokens = estimate_tokens(content)
    assert tokens <= MAX_SKILL_TOKENS, (
        f"Skill '{expected_name}' is ~{tokens} tokens (max {MAX_SKILL_TOKENS}). "
        "Move bulky reference material into supplementary files to prevent context bloat."
    )


@pytest.mark.parametrize("rule_md", get_rule_files(), ids=lambda p: p.name)
def test_rule_frontmatter_and_token_budget(rule_md: Path):
    """Every rule in .agents/rules/*.md must have valid frontmatter and stay within the always-on token budget."""
    content = rule_md.read_text(encoding="utf-8")
    fm = parse_simple_frontmatter(content)
    assert fm is not None, f"{rule_md.relative_to(REPO_ROOT)} is missing YAML frontmatter (--- ... ---)"

    expected_name = rule_md.stem
    assert fm.get("name") == expected_name, (
        f"Rule frontmatter 'name' ({fm.get('name')!r}) must match file stem ({expected_name!r})"
    )

    description = fm.get("description", "")
    assert len(description) >= 20, f"Rule '{expected_name}' must have a descriptive summary (>= 20 chars)."

    tokens = estimate_tokens(content)
    assert tokens <= MAX_RULE_TOKENS, (
        f"Rule '{expected_name}' is ~{tokens} tokens (max {MAX_RULE_TOKENS}). "
        "Always-on rules are injected into every prompt and must remain concise."
    )


@pytest.mark.parametrize("skill_md", get_skill_files(), ids=lambda p: p.parent.name)
def test_claude_and_agents_skill_symlink_parity(skill_md: Path):
    """Every skill in .agents/skills/<name> must have a matching valid symlink in .claude/skills/<name>."""
    skill_dir = skill_md.parent
    skill_name = skill_dir.name

    claude_link = CLAUDE_SKILLS_DIR / skill_name
    assert claude_link.is_symlink(), f"Missing .claude/skills/{skill_name} symlink for Claude Code compatibility"
    assert claude_link.resolve() == skill_dir.resolve(), (
        f".claude/skills/{skill_name} resolves to {claude_link.resolve()}, expected {skill_dir.resolve()}"
    )

    # Verify any bundled scripts inside the skill resolve and have 100755 mode in Git
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.exists():
        for script_path in scripts_dir.iterdir():
            assert script_path.exists(), f"Broken symlink or missing script: {script_path}"
            resolved = script_path.resolve()
            rel_resolved = resolved.relative_to(REPO_ROOT).as_posix()
            ls_files = subprocess.run(
                ["git", "ls-files", "-s", rel_resolved],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            if ls_files:
                mode = ls_files.split()[0]
                assert mode == "100755", (
                    f"Bundled skill script '{rel_resolved}' has Git mode {mode}; expected 100755 (executable)"
                )


@pytest.mark.parametrize("doc_path", get_markdown_docs(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_markdown_links_and_antigravity_includes_resolve(doc_path: Path):
    """All markdown links and @[label](path) includes must be portable and resolve to real repository paths."""
    raw_content = doc_path.read_text(encoding="utf-8")
    # Strip fenced and inline code blocks so template placeholders in code examples are ignored
    stripped = FENCED_CODE_RE.sub("", raw_content)
    stripped = INLINE_CODE_RE.sub("", stripped)

    for match in MD_LINK_RE.finditer(stripped):
        target = match.group(1).strip()
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue

        # Reject machine-local paths
        assert not target.startswith(("file://", "/home/", "/media/", "/Users/")), (
            f"{doc_path.relative_to(REPO_ROOT)} contains non-portable machine-local link: {target!r}"
        )

        # Strip anchor fragment (#L10, #section)
        clean_target = target.split("#", 1)[0]
        if not clean_target:
            continue

        candidate_from_doc = (doc_path.parent / clean_target).resolve()
        candidate_from_root = (REPO_ROOT / clean_target).resolve()
        assert candidate_from_doc.exists() or candidate_from_root.exists(), (
            f"Broken relative link {target!r} in {doc_path.relative_to(REPO_ROOT)}"
        )
