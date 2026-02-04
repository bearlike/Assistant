.PHONY: bootstrap lint lint-fix typecheck precommit-install

VENV ?= .venv

bootstrap:
	uv venv $(VENV)
	uv pip install -e .[dev]
	uv pip install -e packages/meeseeks_core -e packages/meeseeks_tools \
		-e apps/meeseeks_api -e apps/meeseeks_chat -e apps/meeseeks_cli \
		-e meeseeks_ha_conversation

lint:
	$(VENV)/bin/ruff check .

lint-fix:
	$(VENV)/bin/ruff check --fix .

typecheck:
	$(VENV)/bin/mypy

precommit-install:
	$(VENV)/bin/pre-commit install
