#!/usr/bin/env bash
# Rebuild and redeploy the full stack, in the one order that is safe.
#
# Run it through `make redeploy` rather than directly — the Makefile is where
# the prerequisites are checked.
#
# Secrets are fetched from a secrets manager at run time; none are stored in
# this repository. Which project and configuration to read is NOT encoded here
# either: `ssm` resolves that from the directory binding written by
# `make ssm-bootstrap` into a gitignored `.ssm/`, so the deployment's own
# identifiers never enter a tracked file.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# Secrets come from the secrets manager, never from the working tree. There
# are two distinct plumbing legs and they are NOT satisfied by the same
# mechanism:
#
#   (a) compose's own `${VAR}` interpolation (the MEWBO_*_SRC / *_DIST mount
#       paths in docker-compose.override.yml) reads the process environment
#       compose itself runs in.
#   (b) each service's `env_file: ${MEWBO_ENV_FILE:-.env}` names an actual
#       file on disk — `env_file:` cannot read the process environment, only
#       a path — so that leg still needs a materialized file even though (a)
#       does not.
#
# `ssm run -- docker compose ...` below covers (a): it injects the resolved
# secrets straight into docker compose's own environment, no temp file
# involved for that leg. Leg (b) is covered by materializing into a private
# temp directory that shreds on exit, so a checkout — or a stray
# `git add -f` — can never carry the file. `ssm run` inherits the calling
# shell's exported vars (verified: MEWBO_ENV_FILE survives into its child),
# so exporting it once here is enough for every compose call below.
MEWBO_ENV_DIR="$(mktemp -d)"
chmod 700 "$MEWBO_ENV_DIR"
trap 'rm -rf "$MEWBO_ENV_DIR"' EXIT INT TERM
export MEWBO_ENV_FILE="$MEWBO_ENV_DIR/mewbo.env"
# Bounded, because this is the first command in the whole script that can
# print anything AND the first `ssm` call: if it wedges, `make redeploy`
# leaves the operator staring at a blank terminal forever, and every
# safeguard below (the app.json cross-check, the non-empty test, the
# console-first ordering) is downstream of it and never runs. A materialize
# is a fast pre-flight — resolve a token, one HTTP call the CLI already
# bounds — so 120s is far past any healthy run. Only THIS call is bounded:
# the `compose` builds below legitimately take many minutes, and they stream
# docker output continuously, so a stall there is both slow by design and
# visible. Do not "complete the pattern" by wrapping them too.
#
# `|| status=$?` rather than `if ! ...; then status=$?`: inside an `if !`
# body $? is the negated condition (always 0), so that form silently
# swallows the real exit code — verified, not assumed.
status=0
timeout 120 ssm secrets materialize --path "$MEWBO_ENV_FILE" || status=$?
if [ "$status" -ne 0 ]; then
  if [ "$status" -eq 124 ]; then
    echo "ssm secrets materialize timed out after 120s — nothing was deployed." >&2
    echo "Check that the ssm CLI can authenticate: 'ssm secrets keys'." >&2
  fi
  exit "$status"
fi
chmod 600 "$MEWBO_ENV_FILE"
# A materialize that "succeeds" with nothing in it would redeploy the whole
# stack with an empty environment, which comes up healthy and cannot reach
# anything. Refuse instead — the cost of being wrong here is the live stack.
test -s "$MEWBO_ENV_FILE"
grep -q '^MEWBO_MASTER_API_TOKEN=.' "$MEWBO_ENV_FILE"
echo "✅ secrets materialized ($(grep -c '^[A-Z_][A-Z0-9_]*=' "$MEWBO_ENV_FILE") keys)"

# A `${VAR}` in app.json naming a key the store does not carry fails LATE and
# badly: the config validator refuses the unresolved reference, so the api
# crash-loops on a config it only reads at startup — after the rebuild, after
# the old container is gone. Nothing before this point notices, because the
# env file is perfectly valid; it simply lacks a name the config asks for.
# One typo (a missing `MEWBO_` prefix) is all it takes. Check the two sides
# against each other while the running stack is still intact.
if [ -f configs/app.json ]; then
  missing=""
  for ref in $(grep -ohE '\$\{[A-Z0-9_]+\}' configs/app.json | tr -d '${}' | sort -u); do
    grep -q "^${ref}=" "$MEWBO_ENV_FILE" || missing="$missing $ref"
  done
  if [ -n "$missing" ]; then
    echo "configs/app.json references keys the secret store does not carry:$missing" >&2
    echo "Add them to the store, or correct the reference in app.json." >&2
    exit 1
  fi
  echo "✅ every app.json environment reference resolves"
fi

# Every compose call in this script goes through `ssm run` for leg (a); the
# exported MEWBO_ENV_FILE above covers leg (b). Invoking `docker compose`
# directly below would silently drop the interpolation leg (mount paths fall
# back to the repo-relative `./` defaults instead of the resolved sources).
compose() { ssm run -- docker compose "$@"; }

case "${MEWBO_DEPLOY_MODE:-build}" in
  build) ;;
  pull) ;;
  *)
    echo "MEWBO_DEPLOY_MODE must be build or pull." >&2
    exit 2
    ;;
esac

if [ "${MEWBO_DEPLOY_MODE:-build}" = "pull" ]; then
  export MEWBO_PULL_POLICY=always
  compose pull
  compose up -d --force-recreate
  echo "✅ full stack redeployed"
  exit 0
fi

# The console container doesn't rebuild from a Dockerfile — nginx serves the
# bind-mounted apps/mewbo_console/dist directly, and runtime-config.js is
# written into that same directory by docker/console-entrypoint.sh at
# container start, not by the vite build. `npm run build` wipes the whole
# dist/ dir including that file, so restart the console right away, before
# attempting the heavier api/mewbo-mcp rebuild below. This bounds the
# console-outage window to this one step regardless of whether the rest of
# the chain succeeds: otherwise a failed api/mewbo-mcp build leaves the site
# 404ing on runtime-config.js until someone notices and restarts it by hand.
(cd apps/mewbo_console && npm run build)
compose up -d --pull never --force-recreate console
# `up -d` returns once the container starts, not once its entrypoint
# (docker/docker-entrypoint.d/90-runtime-config.sh) finishes writing the
# file — a bare `test -f` here races that and fails most of the time.
# Poll instead: 50 x 0.2s = 10s ceiling, well past the entrypoint's cost.
for _ in $(seq 1 50); do
  test -f apps/mewbo_console/dist/runtime-config.js && break
  sleep 0.2
done
test -f apps/mewbo_console/dist/runtime-config.js
echo "✅ console rebuilt, runtime-config.js present"

# mewbo-base FIRST. api and mewbo-mcp build FROM the image selected by
# their BASE_IMAGE build-arg, and it is not a compose service, so
# `docker compose build` never rebuilds it — it just consumes whatever carries
# that tag locally. Skipping this step is how a redeploy silently produced
# containers on a months-old base: the api/mewbo-mcp layers were new, the OS
# underneath them was not, so anything depending on the base (a newer glibc, a
# newer system package) was missing with nothing in the output to say why.
#
# Cached on purpose, unlike api/mewbo-mcp below: this layer set includes Node and
# Chromium, so --no-cache would add many minutes to every redeploy. When
# Dockerfile.base is unchanged this is a fast no-op; when it changes, the cache
# misses exactly the layers that changed.
docker build -f docker/Dockerfile.base -t "${MEWBO_REGISTRY:-ghcr.io/bearlike}/mewbo-base:${MEWBO_TAG:-latest}" .
echo "✅ mewbo-base rebuilt"

compose build --no-cache api mewbo-mcp

# mewbo-ide (the Web IDE broker) builds CACHED, unlike api/mewbo-mcp above.
# It is a small Node image whose Dockerfile invalidates correctly on its own:
# the COPY of apps/mewbo_ide busts the build layer whenever the source moves,
# and package-lock.json busts `npm ci` whenever the dependencies do. There is
# no uv/pip resolution step here for a stale cache to hide, so --no-cache
# would only re-download node_modules on every redeploy for nothing.
#
# It MUST be listed explicitly: `up -d --pull never` starts whatever image
# already carries the tag and never builds, so leaving it out silently
# redeploys the api against a stale broker. The two sides share a wire
# contract with extra="forbid" on both ends, so a stale broker is not a
# degraded IDE — it is every IDE call failing.
compose build mewbo-ide
echo "✅ mewbo-ide rebuilt"

compose up -d --pull never --force-recreate
echo "✅ full stack redeployed"
