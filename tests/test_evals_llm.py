"""Multi-provider free-tier LLM evaluations and deterministic graders for skills & rules."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_STANDARDS_PATH = REPO_ROOT / ".agents" / "rules" / "code-standards.md"
ENVIRONMENT_ISOLATION_PATH = REPO_ROOT / ".agents" / "rules" / "environment-isolation.md"
ATOMIC_COMMIT_SKILL_PATH = REPO_ROOT / ".agents" / "skills" / "atomic-commit-chain" / "SKILL.md"


@dataclass(frozen=True)
class LLMProviderConfig:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    model: str


def _load_dotenv_local_if_present() -> None:
    """Load untracked .env.local into os.environ if present without printing values."""
    env_local = REPO_ROOT / ".env.local"
    if not env_local.is_file():
        return
    for raw_line in env_local.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


def resolve_llm_provider(env: dict[str, str] | None = None) -> LLMProviderConfig | None:
    """Resolve the active free-tier LLM provider in priority order."""
    if env is None:
        _load_dotenv_local_if_present()
        env = dict(os.environ)

    model_override = env.get("EVAL_MODEL", "").strip()

    if env.get("EVAL_API_KEY") and env.get("EVAL_BASE_URL"):
        return LLMProviderConfig(
            name="custom",
            base_url=env["EVAL_BASE_URL"].strip(),
            api_key=env["EVAL_API_KEY"].strip(),
            model=model_override or "gpt-4o-mini",
        )

    if env.get("GEMINI_API_KEY"):
        return LLMProviderConfig(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            api_key=env["GEMINI_API_KEY"].strip(),
            model=model_override or "gemini-3.5-flash-lite",
        )

    if env.get("GROQ_API_KEY"):
        return LLMProviderConfig(
            name="groq",
            base_url="https://api.groq.com/openai/v1/chat/completions",
            api_key=env["GROQ_API_KEY"].strip(),
            model=model_override or "llama-3.3-70b-versatile",
        )

    if env.get("OPENROUTER_API_KEY"):
        return LLMProviderConfig(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1/chat/completions",
            api_key=env["OPENROUTER_API_KEY"].strip(),
            model=model_override or "meta-llama/llama-3.3-70b-instruct:free",
        )

    if env.get("OLLAMA_MODEL"):
        ollama_host = env.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        return LLMProviderConfig(
            name="ollama",
            base_url=f"{ollama_host}/v1/chat/completions",
            api_key="ollama",
            model=env["OLLAMA_MODEL"].strip(),
        )

    return None


def call_llm_chat(
    system_prompt: str,
    user_prompt: str,
    provider: LLMProviderConfig | None = None,
    temperature: float = 0.0,
) -> str:
    """Execute a zero-dependency OpenAI-compatible chat completion call."""
    if provider is None:
        provider = resolve_llm_provider()
    if provider is None:
        pytest.skip(
            "No LLM eval provider configured. Set GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY, or OLLAMA_MODEL."
        )

    candidate_models = [provider.model]
    if provider.name == "gemini":
        for fallback in ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite"):
            if fallback not in candidate_models:
                candidate_models.append(fallback)

    last_error = ""
    for model_name in candidate_models:
        payload = json.dumps(
            {
                "model": model_name,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            provider.base_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {provider.api_key}",
                "User-Agent": "crenet-governance-evals/0.1",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code} ({model_name}): {err_body}"
            if exc.code in (404, 429, 500, 502, 503, 504):
                continue
            break

    pytest.fail(f"LLM API call to {provider.name} failed across candidate models: {last_error}")


# ---------------------------------------------------------------------------
# Deterministic Graders (used by both offline unit tests and live LLM evals)
# ---------------------------------------------------------------------------


def grade_code_standards_output(output: str) -> list[str]:
    """Deterministically grade generated TypeScript & test code against code-standards.md."""
    violations: list[str] = []

    if re.search(r"\benum\s+[A-Za-z_][A-Za-z0-9_]*", output):
        violations.append("Used prohibited TypeScript `enum` keyword instead of POJO `as const` object.")

    if "as const" not in output:
        violations.append("Missing POJO enum `as const` declaration.")

    if not re.search(r"\b(?:test|it|describe)\.each\b", output):
        violations.append("Missing parameterized test construct (`test.each`, `it.each`, or `describe.each`).")

    if not re.search(r"\b(?:function\s+create[A-Z]\w*|const\s+create[A-Z]\w*\s*=)", output):
        violations.append("Missing reusable fixture factory function (`create<Entity>(overrides)`).")

    return violations


def grade_atomic_commit_plan(output: str) -> list[str]:
    """Deterministically grade an atomic commit plan for topological wave order and human gate."""
    violations: list[str] = []

    shared_pos = output.find("shared/protocol.ts")
    server_pos = output.find("server/handler.ts")

    if shared_pos == -1 or server_pos == -1:
        violations.append("Commit plan must include both `shared/protocol.ts` and `server/handler.ts`.")
    elif shared_pos > server_pos:
        violations.append(
            "Topological violation: `shared/protocol.ts` (Wave 1) must be planned before `server/handler.ts` (Wave 2)."
        )

    if not re.search(r"(?:approv|confirm|proceed|review|shall i|would you like)", output, re.IGNORECASE):
        violations.append("Missing Human Approval Gate prompt before executing commits.")

    return violations


# ---------------------------------------------------------------------------
# Offline Unit Tests for Graders & Provider Resolution (run in `make test`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_map", "expected_name", "expected_model"),
    [
        ({"GEMINI_API_KEY": "test-gem"}, "gemini", "gemini-3.5-flash-lite"),
        ({"GROQ_API_KEY": "test-groq"}, "groq", "llama-3.3-70b-versatile"),
        (
            {"OPENROUTER_API_KEY": "test-or"},
            "openrouter",
            "meta-llama/llama-3.3-70b-instruct:free",
        ),
        ({"OLLAMA_MODEL": "qwen2.5-coder:7b"}, "ollama", "qwen2.5-coder:7b"),
        (
            {"GEMINI_API_KEY": "test-gem", "EVAL_MODEL": "gemini-2.5-pro"},
            "gemini",
            "gemini-2.5-pro",
        ),
        ({}, None, None),
    ],
)
def test_provider_resolution_priority(
    env_map: dict[str, str], expected_name: str | None, expected_model: str | None
):
    """Verifies resolve_llm_provider selects the expected free-tier endpoint and model."""
    provider = resolve_llm_provider(env_map)
    if expected_name is None:
        assert provider is None
    else:
        assert provider is not None
        assert provider.name == expected_name
        assert provider.model == expected_model


@pytest.mark.parametrize(
    ("sample_output", "expected_violation_substring"),
    [
        (
            """
            export const ShipRole = { Interceptor: 'Interceptor', Cruiser: 'Cruiser' } as const;
            export type ShipRole = typeof ShipRole[keyof typeof ShipRole];
            const createPlayer = (overrides = {}) => ({ id: 'p1', role: ShipRole.Interceptor, ...overrides });
            test.each([['Interceptor', 100], ['Cruiser', 250]])('role %s has %i shield', (role, shield) => {});
            """,
            None,
        ),
        (
            """
            export enum ShipRole { Interceptor = 'Interceptor' }
            const createPlayer = () => ({});
            test.each([])('test', () => {});
            """,
            "prohibited TypeScript `enum`",
        ),
        (
            """
            export const ShipRole = { Interceptor: 'Interceptor' } as const;
            test('single non-parameterized test', () => {});
            """,
            "Missing parameterized test",
        ),
    ],
)
def test_grade_code_standards_output_cases(
    sample_output: str, expected_violation_substring: str | None
):
    """Unit-tests the deterministic code-standards grader offline."""
    violations = grade_code_standards_output(sample_output)
    if expected_violation_substring is None:
        assert violations == []
    else:
        assert any(expected_violation_substring in v for v in violations), f"Got: {violations}"


@pytest.mark.parametrize(
    ("sample_plan", "expected_violation_substring"),
    [
        (
            "Commit 1: feat(shared): add protocol types (shared/protocol.ts)\n"
            "Commit 2: feat(server): handle message (server/handler.ts)\n"
            "Please review and confirm if you would like to proceed with Commit 1.",
            None,
        ),
        (
            "Commit 1: server/handler.ts\nCommit 2: shared/protocol.ts\nWould you like to proceed?",
            "Topological violation",
        ),
    ],
)
def test_grade_atomic_commit_plan_cases(
    sample_plan: str, expected_violation_substring: str | None
):
    """Unit-tests the deterministic atomic-commit-chain plan grader offline."""
    violations = grade_atomic_commit_plan(sample_plan)
    if expected_violation_substring is None:
        assert violations == []
    else:
        assert any(expected_violation_substring in v for v in violations), f"Got: {violations}"


# ---------------------------------------------------------------------------
# Live Free-Tier LLM Evaluations (`make test-evals` / `pytest -m llm_eval`)
# ---------------------------------------------------------------------------


@pytest.mark.llm_eval
def test_eval_code_standards_rule_compliance():
    """Evaluates whether an LLM given code-standards.md produces compliant TS POJO enums and Vitest tests."""
    rule_content = CODE_STANDARDS_PATH.read_text(encoding="utf-8")
    system_prompt = (
        "You are a Crenet Games TypeScript engineer. Strictly follow all rules in the governance standard below.\n\n"
        f"{rule_content}"
    )
    user_prompt = (
        "Write a self-contained TypeScript snippet containing:\n"
        "1. A `ShipRole` enumeration with values `Fighter` and `CapitalShip`.\n"
        "2. A `FighterShip` interface with a discriminant `role` property.\n"
        "3. A Vitest test suite testing shield calculations across multiple roles and shield multipliers, "
        "using a reusable player/ship state fixture helper."
    )

    response = call_llm_chat(system_prompt=system_prompt, user_prompt=user_prompt)
    violations = grade_code_standards_output(response)
    assert not violations, f"Model violated code-standards.md:\n" + "\n".join(
        f"- {v}" for v in violations
    ) + f"\n\n--- Model Output ---\n{response}"


@pytest.mark.llm_eval
@pytest.mark.parametrize(
    ("developer_prompt", "expected_target"),
    [
        (
            "I just finished prototyping across 8 files in shared/ and server/. "
            "Can you split my dirty working tree into a clean stack of atomic commits?",
            "atomic-commit-chain",
        ),
        (
            "Before I run git push, verify that my git user.email is allowed and no unauthorized VPN is running.",
            "environment-isolation",
        ),
        (
            "Refactor this vanilla JS UI card to clone an HTML <template> and use a POJO enum instead of TS enum.",
            "code-standards",
        ),
    ],
)
def test_eval_skill_and_rule_routing(developer_prompt: str, expected_target: str):
    """Evaluates whether an LLM accurately routes developer requests to the matching skill or rule."""
    catalog = (
        "Available Crenet Governance customizations:\n"
        "- `atomic-commit-chain`: "
        + ATOMIC_COMMIT_SKILL_PATH.read_text(encoding="utf-8").splitlines()[3]
        + "\n"
        "- `environment-isolation`: Pre-commit strict environment isolation checks & Git config email validation.\n"
        "- `code-standards`: Guidelines for HTML templates, semantic CSS, TypeScript POJO enums, Makefile orchestration, and Jest/Vitest testing standards.\n"
    )
    system_prompt = (
        f"{catalog}\n"
        "Reply with ONLY the exact identifier (`atomic-commit-chain`, `environment-isolation`, or `code-standards`) "
        "that best matches the user's request."
    )

    response = call_llm_chat(system_prompt=system_prompt, user_prompt=developer_prompt).strip()
    assert expected_target in response, (
        f"Expected routing to '{expected_target}' for prompt {developer_prompt!r}, got: {response!r}"
    )


@pytest.mark.llm_eval
def test_eval_atomic_commit_plan_and_human_gate():
    """Evaluates whether an LLM given atomic-commit-chain/SKILL.md respects topological waves and the human gate."""
    skill_content = ATOMIC_COMMIT_SKILL_PATH.read_text(encoding="utf-8")
    mock_status_json = json.dumps(
        {
            "total_dirty_files": 2,
            "files": [
                {
                    "path": "shared/protocol.ts",
                    "category": "1-shared",
                    "depends_on_dirty": [],
                    "unstaged_hunks": 1,
                },
                {
                    "path": "server/handler.ts",
                    "category": "2-domain-server",
                    "depends_on_dirty": ["shared/protocol.ts"],
                    "unstaged_hunks": 1,
                },
            ],
            "topological_waves": [["shared/protocol.ts"], ["server/handler.ts"]],
            "mutual_dependency_cycles": [],
        },
        indent=2,
    )

    system_prompt = (
        "You are executing the `atomic-commit-chain` skill. Follow its workflow phases and gates strictly.\n\n"
        f"{skill_content}"
    )
    user_prompt = (
        "Here is the output of `atomic_commit_status`:\n"
        f"```json\n{mock_status_json}\n```\n"
        "Perform Step 1 & Step 2 now (propose the ordered commit chain and pause at the required gate)."
    )

    response = call_llm_chat(system_prompt=system_prompt, user_prompt=user_prompt)
    violations = grade_atomic_commit_plan(response)
    assert not violations, f"Model violated atomic-commit-chain workflow:\n" + "\n".join(
        f"- {v}" for v in violations
    ) + f"\n\n--- Model Output ---\n{response}"
