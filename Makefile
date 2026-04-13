.PHONY: bootstrap lint lint-fix typecheck precommit-install vendor-aider docs docs-build

VENV ?= .venv
DOCS_ADDR ?= 0.0.0.0:8000

bootstrap:
	uv venv $(VENV)
	uv pip install -e .[dev]
	uv pip install -e packages/meeseeks_core -e packages/meeseeks_tools \
		-e apps/meeseeks_api -e apps/meeseeks_cli \
		-e meeseeks_ha_conversation

lint:
	$(VENV)/bin/ruff check .

lint-fix:
	$(VENV)/bin/ruff check --fix .

typecheck:
	$(VENV)/bin/mypy

precommit-install:
	$(VENV)/bin/pre-commit install

vendor-aider:
	./scripts/vendor_aider.sh

docs:
	uv run --group docs mkdocs serve --dev-addr $(DOCS_ADDR)

docs-build:
	uv run --group docs mkdocs build --strict
