.PHONY: install dev lint test

install:          ## Install package in editable mode
	uv pip install -e .

dev:              ## Start the API server
	uv run truss-api

lint:             ## Run ruff on source
	ruff check src/

test:             ## Run tests
	uv run pytest tests/ -q
