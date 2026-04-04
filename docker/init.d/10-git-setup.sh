#!/bin/sh
# 10-git-setup.sh — Configure git for non-interactive container use.
#
# credential.helper: bridges GITHUB_TOKEN (via gh CLI) to git auth,
#   so git fetch/push/pull work without TTY prompts.
# safe.directory '*': trusts all volume-mounted repos regardless of
#   file ownership (operator explicitly mounts these in compose).

if command -v gh >/dev/null 2>&1 && [ -n "${GITHUB_TOKEN:-}" ]; then
    git config --global credential.helper '!gh auth git-credential'
fi

git config --global safe.directory '*'
