#!/bin/bash

# This script is used to automate the process of building and installing
# local dependencies for the Meeseeks project. It accepts an argument
# to decide which submodule to install or if all submodules need to be installed.

# Usage:
# ./build-install.sh all                # Build and install all submodules
# ./build-install.sh api                # Build and install the meeseeks-api submodule
# ./build-install.sh chat               # Build and install the meeseeks-chat submodule
# ./build-install.sh cli                # Build and install the meeseeks-cli submodule
# ./build-install.sh fallback-install   # Install submodules when on non package mode

function print_usage {
    echo "Usage: $0 {fallback-install|all|api|chat|cli}"
    echo "fallback-install: Install submodules when on non package mode"
    # ! Commented since not fully baked yet.
    # echo "all: Build and install all submodules"
    # echo "api: Build and install the meeseeks-api submodule"
    # echo "chat: Build and install the meeseeks-chat submodule"
}

function build_and_install() {
    # Navigate to the submodule directory
    script_dir=$(dirname "$(readlink -f "$0")")

    cd "$script_dir/$1"

    # Build the package
    if poetry build; then
        # Move the wheel file to the root directory
        mv dist/*.whl ../

        # Install the package
        if ! pip install ../$1-1.0.0-py3-none-any.whl; then
            echo "Error: Failed to install the $1 package"
            exit 1
        fi
    else
        echo "Error: Failed to build the $1 package"
        exit 1
    fi

    # Clean up unwanted files and folders
    rm -rf dist
    rm -rf $1.egg-info

    # Navigate back to the root directory
    cd ..
}

# It manually installs dependencies when in non-package mode.
fallback_install() {
    # TODO: Fallback for when non package mode installation fails.
    # Get the absolute path of the script directory
    script_dir=$(dirname "$(readlink -f "$0")")
    # Log the file being installed
    # Navigate to the submodule directory
    cd "$script_dir/$1"
    echo "Installing $script_dir/$1"

    # Install the dependencies
    if ! poetry install; then
        echo "Error: Failed to install the dependencies for the $1 module"
        exit 1
    fi

    # Navigate back to the root directory
    cd "$script_dir"
}


if [ "$#" -ne 1 ]; then
    echo "Error: Invalid number of arguments"
    print_usage
    exit 1
fi



# Update the case statement in your build.sh script to support 'fallback-install' and 'fallback-build' arguments
case $1 in
    all)
        build_and_install "meeseeks-api"
        build_and_install "meeseeks-chat"
        build_and_install "meeseeks-cli"
        poetry install --extras "api chat cli"
        ;;
    api)
        build_and_install "meeseeks-api"
        poetry install --extras "api"
        ;;
    chat)
        build_and_install "meeseeks-chat"
        poetry install --extras "chat"
        ;;
    cli)
        build_and_install "meeseeks-cli"
        poetry install --extras "cli"
        ;;
    fallback-install)
        fallback_install "."
        fallback_install "meeseeks-api"
        fallback_install "meeseeks-chat"
        fallback_install "meeseeks-cli"
        ;;
    *)
        echo "Error: Invalid argument"
        print_usage
        exit 1
        ;;
esac
