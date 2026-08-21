# syntax=docker/dockerfile:1
#
# Tier 0 image for the devcontainer.
#
# WHY THIS MIRRORS docker/Dockerfile.base INSTEAD OF INHERITING FROM IT
#
# The obvious shape is `FROM mewbo-base:...` over an image built from
# docker/Dockerfile.base. It does not survive the launcher. The workspace
# supervisor resolves this config with
# `devcontainer read-configuration --include-merged-configuration` BEFORE it
# ever brings a container up, and that resolution inspects the FROM image so it
# can merge the image's own devcontainer metadata. A locally-built tag does not
# exist yet at that moment, and nothing that runs at create time (not even
# `initializeCommand`, which does run before `up`) happens early enough to
# create it. Cold resolution fails, and the workspace never starts. Pulling the
# published base instead is no better: it is not anonymously pullable, and it
# lags whatever is in the checkout.
#
# So this file re-derives the base's TOOLCHAIN from the same public parent.
# Two consequences worth knowing before editing either file:
#
#   1. The FROM line is the part that must stay in lockstep. python:3.11-trixie
#      is chosen for glibc 2.41, not for Python: prebuilt Rust CLIs the runtime
#      fetches import pidfd_spawnp/pidfd_getpid and so record a GLIBC_2.39
#      requirement, which bookworm's 2.36 cannot satisfy — they install and then
#      die on first use. If docker/Dockerfile.base moves its base, move this too.
#   2. The divergences below are deliberate, not drift. Each drops a step that
#      something else in this devcontainer already owns; see the notes inline.
FROM python:3.11-trixie

# Same identity as docker/Dockerfile.base, so a path-identical workspace mount
# lands on a matching uid. The devcontainer CLI remaps this to the host uid at
# create time when they differ, so 1000 is a default rather than an assumption.
ARG USERNAME=mewbo
ARG USER_UID=1000
ARG USER_GID=$USER_UID

# ---------- System packages ----------
# First group: mirrors docker/Dockerfile.base's toolchain verbatim.
#
# Second group is devcontainer-only. iptables + iproute2 are HARD requirements
# twice over: the workspace supervisor installs an egress allowlist as an
# in-container iptables ruleset ending in `-P OUTPUT DROP` and then verifies the
# drop, so a container missing either binary ABORTS at start — AND the nested
# dockerd needs both to build its own bridge network and NAT its children out.
# tmux is the attach surface for an interactive session; procps and
# iputils-ping exist so the reachability checks in init.d have something to run.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl wget git zsh sudo \
        jq ripgrep fd-find less vim \
        openssh-client ca-certificates gnupg \
        iptables iproute2 \
        tmux procps iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# iptables lives in /usr/sbin, which Debian leaves off a NON-root user's PATH.
# The egress script runs as the container user, so without this a bare
# `iptables` call fails with "command not found" on an image that demonstrably
# has it installed — a confusing way to lose a container start. (`ip` is
# unaffected: iproute2 also links it into /usr/bin.)
ENV PATH="${PATH}:/usr/sbin:/sbin"

# ---------- Non-root user with sudo ----------
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /usr/bin/zsh $USERNAME \
    && echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/$USERNAME \
    && chmod 0440 /etc/sudoers.d/$USERNAME

# ---------- Python: uv ----------
# uv only. docker/Dockerfile.base additionally does `uv pip install --system -e
# packages/mewbo_core packages/mewbo_tools`, and that step is deliberately NOT
# mirrored: baking a resolved dependency closure into the image would mean every
# uv.lock bump costs an image rebuild, which is the exact cost the tier model
# exists to avoid. Dependency install is Tier 2 (`uv sync`, from init.d).
# Skipping it also keeps the build context at this directory instead of the
# monorepo root.
RUN pip install --no-cache-dir uv

# ---------- Simple Secrets Manager (ssm / ssm-cli) ----------
# The workspace agent is launched through `ssm run`, so an image without it on
# PATH boots the agent straight into exit 127. Installed here (Tier 0) rather
# than init.d because it is toolchain, not project state.
#
# All three UV_* paths are load-bearing, because this installs as root while
# the agent runs as $USERNAME:
#   BIN_DIR     — uv's default (~/.local/bin) is NOT on a non-login shell's
#                 PATH, and the agent pane runs `sh -c`, so a default install
#                 is present and still unfindable (exit 127).
#   TOOL_DIR    — the shim is a symlink INTO the tool venv; left at the default
#                 it points inside /root and the agent gets exit 126.
#   PYTHON_...  — the venv's interpreter is likewise fetched under /root by
#                 default, so the venv resolves to an unreadable python.
# `a+rX` then grants traverse/read without marking data files executable. The
# python dir is only created when uv had to FETCH an interpreter; this image
# already ships one it can reuse, so the loop skips what does not exist rather
# than failing the build on it.
#
# The null keyring backend is required at RUNTIME, not just here: a container
# has no secret service, and keyring's search for one hangs a headless client.
# The CLI reads SSM_BASE_URL/SSM_TOKEN from the environment, which the
# workspace supervisor already injects — so no credential file is baked in.
ENV PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
RUN UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    uv tool install git+https://github.com/bearlike/Simple-Secrets-Manager.git \
    && for d in /opt/uv-python /opt/uv-tools; do [ -d "$d" ] && chmod -R a+rX "$d"; done

# ---------- Shared cache mount points ----------
# Named volumes are mounted here by devcontainer.json. Create and chown them at
# build time: a volume is created empty and root-owned on first use, and the
# non-root container user cannot repair that without a sudo round-trip on every
# start.
#
# Only concurrency-safe, content-addressed caches live here. node_modules and
# .venv deliberately do NOT — several workspaces of this repo run concurrent
# containers against these same volumes, and a non-atomic writer would corrupt
# them for its neighbours. Those stay in the worktree, which is already a
# per-workspace bind mount.
RUN mkdir -p /caches/uv /caches/npm /caches/playwright \
    && chown -R "$USERNAME:$USERNAME" /caches

# ---------- Deliberately absent ----------
# node/nvm         → the node:2 feature installs it, pinned to 20 to match CI.
#                    docker/Dockerfile.base's `nvm install --lts` floats.
# oh-my-zsh        → the common-utils feature installs it.
# docker CLI+daemon → the docker-in-docker feature installs BOTH; two installs
#                    would leave two versions racing on PATH. That feature is
#                    also what makes this container self-sufficient: it runs a
#                    dockerd of its own instead of borrowing the host's.
# Playwright       → same reasoning docker/Dockerfile.base gives for dropping
#                    it: a ~2 GB layer no gate command in this repo needs.
#                    `npx playwright install --with-deps` puts browsers in the
#                    shared cache volume on demand.
# runtime ENV      → CACHE_DIR/DEFAULT_MODEL/LOG_LEVEL/VERSION and friends are
#                    deployment policy. They belong in configs/app.json and the
#                    env files that already carry them, not baked into a dev image.

USER $USERNAME
