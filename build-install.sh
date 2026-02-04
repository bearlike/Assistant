#!/bin/bash

set -euo pipefail

# Usage:
# ./build-install.sh all   # Create venv + install all packages and dev deps
# ./build-install.sh api   # Install API package
# ./build-install.sh chat  # Install Chat package
# ./build-install.sh cli   # Install CLI package
# ./build-install.sh core  # Install core package
# ./build-install.sh tools # Install tools package
# ./build-install.sh ha    # Install Home Assistant integration

function print_usage {
    echo "Usage: $0 {all|api|chat|cli|core|tools|ha}"
}

install_all() {
    uv venv .venv
    uv pip install -e .[dev]
    uv pip install -e packages/meeseeks_core -e packages/meeseeks_tools \
        -e apps/meeseeks_api -e apps/meeseeks_chat -e apps/meeseeks_cli \
        -e meeseeks_ha_conversation
}

case ${1:-} in
    all)
        install_all
        ;;
    api)
        uv pip install -e apps/meeseeks_api
        ;;
    chat)
        uv pip install -e apps/meeseeks_chat
        ;;
    cli)
        uv pip install -e apps/meeseeks_cli
        ;;
    core)
        uv pip install -e packages/meeseeks_core
        ;;
    tools)
        uv pip install -e packages/meeseeks_tools
        ;;
    ha)
        uv pip install -e meeseeks_ha_conversation
        ;;
    *)
        print_usage
        exit 1
        ;;
 esac
