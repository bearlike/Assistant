.PHONY: install dev lint test

install:          ## Install package in editable mode
	uv pip install -e .

dev:              ## Start the CLI
	uv run mewbo

lint:             ## Run ruff on source
	ruff check src/

test:             ## Run tests
	uv run pytest tests/ -q
