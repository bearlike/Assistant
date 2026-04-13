#!/bin/sh
# entrypoint-api.sh — Post-installation init, then exec gunicorn.
#
# Scans INIT_DIR for *.sh scripts (sorted by filename for ordering)
# and sources each one. Scripts can export env vars that the app inherits.
# A failing script logs a warning but does NOT prevent startup.
#
# Extend by mounting additional scripts into the init.d directory:
#   volumes:
#     - ./my-init.sh:/app/docker/init.d/20-my-init.sh:ro

set -u

# Named Docker volumes AND bind mounts whose host directory was
# auto-created by docker mount as root:root even when the container runs
# as a non-root user.  Fix ownership of writable directories that need it.
# The meeseeks user has passwordless sudo (see Dockerfile.base).
# /tmp/meeseeks-ide holds per-session deadline files written by the Web
# IDE feature; it is bind-mounted from the host so docker can expose the
# same paths to sibling code-server containers.
for _dir in /tmp/meeseeks /app/data /tmp/meeseeks-ide; do
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

printf '[entrypoint] Starting gunicorn on port %s\n' "${API_PORT:-5125}"
exec gunicorn \
    --bind "0.0.0.0:${API_PORT:-5125}" \
    --workers 1 \
    --threads 8 \
    --timeout 300 \
    --graceful-timeout 30 \
    --access-logfile - \
    meeseeks_api.backend:app
