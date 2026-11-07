.PHONY: test lint-skills test-evals test-all

PYTEST ?= $(if $(wildcard .venv/bin/pytest),.venv/bin/pytest,uv run pytest)

## Run the full deterministic test suite (unit tests + static skill/rule linter)
test:
	$(PYTEST) -m "not llm_eval" -v

## Run only the static skill, rule, and documentation linter
lint-skills:
	$(PYTEST) tests/test_skill_and_rule_lint.py -v

## Run live LLM evaluations (loads untracked .env.local if present, or uses exported env vars)
test-evals:
	@set -a; [ -f .env.local ] && . ./.env.local; set +a; \
	$(PYTEST) -m "llm_eval" -v -s

## Run both deterministic tests and live LLM evaluations
test-all: test test-evals
