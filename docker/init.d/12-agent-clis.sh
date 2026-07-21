#!/bin/sh
# 12-agent-clis.sh — Provision the forge CLIs into the runtime toolbox.
#
# gh  (GitHub CLI) — github.com only; git auth bridges through it in
#     10-git-setup.sh when GITHUB_TOKEN is set.
# tea (Gitea CLI)  — every other forge; logins are derived in 15-tea-setup.sh,
#     which is why this runs BEFORE it (12 < 15). Installing and authenticating
#     in one script is what let the same download get duplicated into
#     Dockerfile.api; keeping the two concerns in two scripts keeps one
#     installer.
#
# Both ship static release binaries, so neither needs apt, a keyring, an apt
# source list, or root — which is the whole reason they can live at runtime.
# Versions are overridable from docker.env for a pin or a rollback.

GH_VERSION="${GH_VERSION:-2.96.0}"
TEA_VERSION="${TEA_VERSION:-0.14.0}"

_cli_arch="$(mewbo_arch)"

mewbo_fetch_bin gh \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${_cli_arch}.tar.gz" \
    "gh_${GH_VERSION}_linux_${_cli_arch}/bin/gh"

# dl.gitea.com, NOT gitea.com/gitea/tea/releases/download/... — the latter is a
# release attachment served by the Gitea web app itself and returns intermittent
# 503/504s (that is the exact URL whose outage failed the api image build).
# dl.gitea.com is Gitea's object-store-backed download CDN: same official
# binaries, no application server in the request path. There is no GitHub mirror
# of tea to fall back to, so the CDN is the reliable source.
mewbo_fetch_bin tea \
    "https://dl.gitea.com/tea/${TEA_VERSION}/tea-${TEA_VERSION}-linux-${_cli_arch}"
