#!/bin/bash

set -euo pipefail

# Usage:
# ./build-install.sh all   # Create venv + install all packages and dev deps
# ./build-install.sh api   # Install API package
# ./build-install.sh cli   # Install CLI package
# ./build-install.sh core  # Install core package
# ./build-install.sh tools # Install tools package
# ./build-install.sh ha    # Install Home Assistant integration

function print_usage {
    echo "Usage: $0 {all|api|console|cli|core|tools|ha}"
}

install_all() {
    uv venv .venv
    uv pip install -e .[dev]
    uv pip install -e packages/truss_core -e packages/truss_tools \
        -e apps/truss_api -e apps/truss_cli \
        -e truss_ha_conversation
}

case ${1:-} in
    all)
        install_all
        ;;
    api)
        uv pip install -e apps/truss_api
        ;;
    console)
        cd apps/truss_console && npm install
        ;;
    cli)
        uv pip install -e apps/truss_cli
        ;;
    core)
        uv pip install -e packages/truss_core
        ;;
    tools)
        uv pip install -e packages/truss_tools
        ;;
    ha)
        uv pip install -e truss_ha_conversation
        ;;
    *)
        print_usage
        exit 1
        ;;
 esac
