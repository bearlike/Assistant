#!/bin/sh
# 10-git-setup.sh — Configure git for non-interactive container use, host-agnostic.
#
# Plugin marketplace catalogs are cloned with plain `git clone`, so they inherit
# the auth + TLS configured here for ANY host (Gitea, GitLab, Forgejo, GHE, …):
#
#   credential.helper 'store' — reads a mounted ~/.git-credentials
#     (https://user:token@host) for any host, without TTY prompts.
#   GITHUB_TOKEN bridge (optional) — scoped to github.com; only activates when
#     both gh and GITHUB_TOKEN are present, other hosts fall through to 'store'.
#   SSH — mount an SSH key / agent socket; git uses it automatically.
#   Self-signed CA — set GIT_SSL_CAINFO=/path/to/ca.pem (git reads it from the
#     environment); verification stays on, no global GIT_SSL_NO_VERIFY.
#   safe.directory '*' — trust all volume-mounted repos regardless of ownership.

# Host-agnostic: let git read credentials for any host from a mounted store.
git config --global credential.helper store

# GitHub convenience bridge (optional, additive), scoped so it never shadows
# credentials for other hosts.
if command -v gh >/dev/null 2>&1 && [ -n "${GITHUB_TOKEN:-}" ]; then
    git config --global credential.https://github.com.helper '!gh auth git-credential'
fi

git config --global safe.directory '*'
