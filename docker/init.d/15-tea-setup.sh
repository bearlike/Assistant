#!/bin/sh
# 15-tea-setup.sh — Install + authenticate tea (Gitea's official CLI).
#
# Mirrors the gh split: the binary ships in the image (Dockerfile.api installs
# it next to gh) and this script owns the runtime concerns — install-if-missing
# covers images built before tea was added, and logins are (re)derived every
# start from tokens already mounted into the container:
#
#   channels.vcs.tokens (configs/app.json) — preferred. These are the
#     assistant's OWN forge identities (the bot account), so tea-driven PRs
#     and comments are authored by the bot, matching the pickup reply leg.
#     Needs scopes read:user (login) + write:repository (PRs) + write:issue.
#   ~/.git-credentials — per-host fallback (minus github.com, which gh owns);
#     the same store 10-git-setup.sh points git at.
#
# Runs after 05-trust-internal-ca.sh, so tea (Go, honors SSL_CERT_FILE)
# verifies internal-CA hosts. Non-Gitea hosts fail `tea login add` and are
# skipped with a warning — never fatal, matching the entrypoint contract.

TEA_VERSION="${TEA_VERSION:-0.14.0}"

if ! command -v tea >/dev/null 2>&1; then
    _tea_arch=$(dpkg --print-architecture 2>/dev/null || uname -m)
    case "$_tea_arch" in x86_64) _tea_arch=amd64 ;; aarch64) _tea_arch=arm64 ;; esac
    _tea_url="https://gitea.com/gitea/tea/releases/download/v${TEA_VERSION}/tea-${TEA_VERSION}-linux-${_tea_arch}"
    if curl -fsSL "$_tea_url" -o /tmp/tea-dl && sudo install -m 0755 /tmp/tea-dl /usr/local/bin/tea; then
        printf ' installed tea %s;' "$TEA_VERSION"
    else
        printf ' tea download failed (%s), skipping;' "$_tea_url" >&2
    fi
    rm -f /tmp/tea-dl
fi

if command -v tea >/dev/null 2>&1; then
    # host<TAB>token pairs: vcs tokens win, git-credentials fill the gaps.
    _tea_pairs=$(python3 - <<'PY'
import json, os, re

pairs: dict[str, str] = {}
try:
    cfg = json.load(open(os.environ.get("MEWBO_APP_CONFIG", "/app/configs/app.json")))
    vcs = (cfg.get("channels") or {}).get("vcs") or {}
    for host, tok in (vcs.get("tokens") or {}).items():
        if tok:
            pairs[host] = tok
except Exception:
    pass
try:
    with open(os.path.expanduser("~/.git-credentials")) as fh:
        for line in fh:
            m = re.match(r"https://([^:]+):([^@]+)@(.+?)/?$", line.strip())
            if m and m.group(3) != "github.com":
                pairs.setdefault(m.group(3), m.group(2))
except Exception:
    pass
for host, tok in pairs.items():
    print(f"{host}\t{tok}")
PY
)
    echo "${_tea_pairs:-}" | while IFS="$(printf '\t')" read -r _tea_host _tea_token; do
        [ -n "${_tea_host:-}" ] && [ -n "${_tea_token:-}" ] || continue
        if tea logins list 2>/dev/null | grep -q "https://${_tea_host}"; then
            continue
        fi
        if tea login add --name "$_tea_host" --url "https://${_tea_host}" --token "$_tea_token" >/dev/null 2>&1; then
            printf ' tea login: %s;' "$_tea_host"
        else
            printf ' tea login FAILED for %s (not Gitea, or token lacks read:user);' "$_tea_host" >&2
        fi
    done
fi
