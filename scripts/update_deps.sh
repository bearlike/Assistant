#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Syncing all extras + groups with uv..."
uv sync --all-extras --all-groups
