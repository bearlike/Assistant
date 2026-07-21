#!/bin/sh
# 00-toolbox.sh — Shared helpers for provisioning CLI tools at runtime.
#
# Defines functions only; provisions nothing itself. Runs first (00-) because
# the entrypoint SOURCES init scripts into one shell, so every later script
# inherits these helpers — that sourcing contract is what makes a single
# download path possible instead of one bespoke curl per tool.
#
# Why runtime and not the image: `gh` and `tea` are identity tools. They are
# inert without the credentials that already arrive at runtime (mounted tokens,
# configs/app.json), so pinning their binaries to build time bought nothing and
# made every image build depend on cli.github.com and gitea.com being up — a
# transient 503 from either one failed the whole api build.
#
# The toolbox is a persistent named volume, so a tool is fetched ONCE ever, not
# once per container start: no per-start network dependency after the first.

MEWBO_TOOLBOX="${MEWBO_TOOLBOX:-/opt/mewbo-tools}"
MEWBO_TOOLBOX_BIN="${MEWBO_TOOLBOX}/bin"

# Debian arch name (amd64/arm64) — the naming both gh and tea use in their
# release assets. Falls back to uname for non-Debian bases.
mewbo_arch() {
    _ma=$(dpkg --print-architecture 2>/dev/null || uname -m)
    case "$_ma" in
        x86_64) _ma=amd64 ;;
        aarch64) _ma=arm64 ;;
    esac
    printf '%s' "$_ma"
}

# mewbo_fetch_bin <name> <url> [tar_member]
#
# Installs <name> into the toolbox unless it is ALREADY on PATH — which makes
# this idempotent across restarts and keeps it a no-op on any image that still
# bakes the tool in. With <tar_member> the URL is treated as a .tar.gz and that
# member is extracted; without it the URL is the raw binary.
#
# Never fatal: a download failure warns and returns 0, because the entrypoint
# contract is that init never blocks startup — a missing `gh` degrades one
# feature, a dead container degrades all of them.
mewbo_fetch_bin() {
    _fb_name="$1"
    _fb_url="$2"
    _fb_member="${3:-}"

    if command -v "$_fb_name" >/dev/null 2>&1; then
        return 0
    fi

    if ! mkdir -p "$MEWBO_TOOLBOX_BIN" 2>/dev/null; then
        printf ' %s: toolbox %s not writable, skipping;' \
            "$_fb_name" "$MEWBO_TOOLBOX_BIN" >&2
        return 0
    fi

    _fb_tmp="$(mktemp)" || return 0

    # Three attempts: gitea.com in particular serves intermittent 5xx, and a
    # single miss would otherwise cost the container its forge CLI until the
    # next restart. Cheap here in a way it never was at build time, where the
    # same miss failed the image.
    _fb_ok=0
    for _fb_try in 1 2 3; do
        if curl -fsSL --connect-timeout 10 --max-time 120 "$_fb_url" -o "$_fb_tmp"; then
            _fb_ok=1
            break
        fi
        [ "$_fb_try" -lt 3 ] && sleep 3
    done
    if [ "$_fb_ok" -eq 0 ]; then
        printf ' %s: download failed after 3 tries (%s), skipping;' \
            "$_fb_name" "$_fb_url" >&2
        rm -f "$_fb_tmp"
        return 0
    fi

    if [ -n "$_fb_member" ]; then
        # -O streams the member to stdout, so no strip-components arithmetic
        # and no temp directory to clean up.
        if ! tar -xzOf "$_fb_tmp" "$_fb_member" > "$MEWBO_TOOLBOX_BIN/$_fb_name" 2>/dev/null; then
            printf ' %s: member %s missing from archive, skipping;' \
                "$_fb_name" "$_fb_member" >&2
            rm -f "$_fb_tmp" "$MEWBO_TOOLBOX_BIN/$_fb_name"
            return 0
        fi
    else
        mv "$_fb_tmp" "$MEWBO_TOOLBOX_BIN/$_fb_name"
    fi

    # 0755 explicitly, NOT `chmod +x`. The raw-binary path lands via mktemp
    # (0600), so `+x` would leave 0711 — executable but unreadable to group and
    # other, unlike the 0755 the tar path happens to produce. The api and mcp
    # containers share this volume and may install as different uids, so the
    # mode has to be stated rather than inherited from a temp file's umask.
    chmod 0755 "$MEWBO_TOOLBOX_BIN/$_fb_name"
    rm -f "$_fb_tmp"
    printf ' installed %s;' "$_fb_name"
}
