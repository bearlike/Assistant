#!/usr/bin/env bash
# Bake redroid/redroid:13.0.0_mindthegapps — the image behind the
# `redroid-gms` compose service (see docker-compose.yml).
#
# Uses ayasa520/redroid-script to layer MindTheGapps (the maintained GApps
# distribution for Android 13) onto the stock redroid 13 image. Rerunning
# rebuilds from the latest MindTheGapps package.
set -euo pipefail

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

git clone --depth 1 https://github.com/ayasa520/redroid-script.git \
  "$WORKDIR/redroid-script"
cd "$WORKDIR/redroid-script"

# deps are just requests+tqdm; uv avoids touching the system python
uv run --with requests --with tqdm python3 redroid.py -a 13.0.0 -mtg -c docker

docker image inspect redroid/redroid:13.0.0_mindthegapps >/dev/null
echo "OK: redroid/redroid:13.0.0_mindthegapps built."
echo "Start it with: docker compose --profile gms up -d"
