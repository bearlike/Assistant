#!/bin/sh
# entrypoint.sh — Shared runtime init for the Python images, then exec the CMD.
#
# Two images use this (api, mcp); the command they start is their own CMD, so
# this file owns only what is common: writable-path repair, then the ordered
# init.d pass. Anything image-specific belongs in a CMD or an init.d script,
# never here.
#
# Scans INIT_DIR for *.sh scripts (sorted by filename for ordering) and SOURCES
# each one — sourcing, not executing, is load-bearing: it is what lets a script
# export an env var (SSL_CERT_FILE, PATH additions) that the app inherits.
# A failing script logs a warning but does NOT prevent startup.
#
# Extend by mounting additional scripts into the init.d directory:
#   volumes:
#     - ./my-init.sh:/app/docker/init.d/20-my-init.sh:ro
#
# The image bakes the tracked scripts, so a deployment with no override still
# gets git/toolbox setup; mounting the directory shadows the baked copies.

set -u

# Named Docker volumes AND bind mounts whose host directory was auto-created by
# docker mount as root:root even when the container runs as a non-root user.
# Fix ownership of writable directories that need it. The mewbo user has
# passwordless sudo (see Dockerfile.base).
#
# Image-specific, because the mcp image has none of the api's scratch volumes:
# each Dockerfile sets INIT_CHOWN_DIRS to the paths it actually mounts, so mcp
# does not warn about api-only directories that will never exist there.
# /tmp/mewbo-ide holds per-session deadline files written by the Web IDE
# feature; it is bind-mounted from the host so docker can expose the same paths
# to sibling code-server containers.
for _dir in ${INIT_CHOWN_DIRS:-}; do
    if [ -d "$_dir" ] && [ ! -w "$_dir" ]; then
        printf '[entrypoint] Fixing ownership on %s\n' "$_dir"
        sudo chown -R "$(id -u):$(id -g)" "$_dir"
    fi
done

INIT_DIR="${INIT_DIR:-/app/docker/init.d}"

if [ -d "$INIT_DIR" ]; then
    for f in $(find "$INIT_DIR" -maxdepth 1 -name '*.sh' -type f 2>/dev/null | sort -V); do
        if [ -r "$f" ]; then
            printf '[init] %s ...' "$(basename "$f")"
            set +e
            . "$f"
            rc=$?
            set -u
            if [ $rc -ne 0 ]; then
                printf ' FAILED (exit %d, continuing)\n' "$rc" >&2
            else
                printf ' ok\n'
            fi
        fi
    done
fi

printf '[entrypoint] Starting: %s\n' "$*"
exec "$@"
