.PHONY: test lint-skills test-evals test-all

PYTEST ?= $(shell if [ -x .venv/bin/pytest ] && .venv/bin/pytest --version >/dev/null 2>&1; then echo .venv/bin/pytest; elif command -v uv >/dev/null 2>&1; then echo "uv run pytest"; else echo "PYTHONPATH=.venv/lib/python3.12/site-packages:mcp-server:scripts python3 -m pytest"; fi)

test:
	$(PYTEST) -m "not llm_eval" -v

lint-skills:
	$(PYTEST) tests/test_skill_and_rule_lint.py -v

test-evals:
	$(PYTEST) -m llm_eval -v -s

test-all:
	$(PYTEST) -v
