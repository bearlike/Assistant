#!/usr/bin/env bash
set -euo pipefail

uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest
