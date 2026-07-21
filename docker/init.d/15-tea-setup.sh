#!/bin/sh
# 15-tea-setup.sh — Authenticate tea (Gitea's official CLI).
#
# Authentication ONLY; 12-agent-clis.sh installs the binary. The two were once
# one script that also duplicated the download into Dockerfile.api, so the same
# fetch existed twice and a gitea.com 503 could fail an image build.
#
# Logins are (re)derived every start from tokens already mounted into the
# container:
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
