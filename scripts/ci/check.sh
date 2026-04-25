#!/usr/bin/env bash
set -euo pipefail

# Mono-repo aware pre-push check script.
# Detects which areas changed and runs only the relevant checks.

repo_root="$(git rev-parse --show-toplevel)"
cd "${repo_root}"

merge_base="$(git merge-base HEAD origin/main 2>/dev/null || echo HEAD~1)"
changed_files="$(git diff --name-only "${merge_base}"..HEAD 2>/dev/null || git diff --name-only HEAD)"

has_python=false
has_console=false

while IFS= read -r file; do
  case "${file}" in
    *.py|*pyproject.toml) has_python=true ;;
    apps/mewbo_console/*) has_console=true ;;
  esac
done <<< "${changed_files}"

# Run Python checks when Python files changed (or when detection yields nothing).
if [[ "${has_python}" == true ]] || [[ -z "${changed_files}" ]]; then
  echo "==> Running Python checks..."
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy
  uv run pytest
fi

# Run Console checks when frontend files changed.
if [[ "${has_console}" == true ]]; then
  echo "==> Running Console checks..."
  cd apps/mewbo_console
  npm run lint:ci
  npm run typecheck
  npm run test:ci
  npm run build
  cd "${repo_root}"
fi

if [[ "${has_python}" == false ]] && [[ "${has_console}" == false ]]; then
  echo "==> No Python or Console changes detected, skipping checks."
fi
