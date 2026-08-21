.PHONY: ssm-bootstrap redeploy bootstrap lint lint-fix typecheck precommit-install vendor-aider openapi docs docs-build aura-apk aura-release aura-install demo-build demo-stage demo-sync demo-up demo-seed demo-shots-web demo-down demo test-mongo

VENV ?= .venv
DOCS_ADDR ?= 0.0.0.0:8000
ANDROID_HOME ?= $(HOME)/android-sdk
AURA_DIR := apps/mewbo_aura
AURA_SERIAL ?= localhost:5555
DEMO_HOST_UID ?= $(shell id -u)
DEMO_HOST_GID ?= $(shell id -g)
DEMO_REPO_VOLUME ?= mewbo-demo-repo
DEMO_COMPOSE := DEMO_HOST_UID=$(DEMO_HOST_UID) DEMO_HOST_GID=$(DEMO_HOST_GID) \
	DEMO_REPO_VOLUME=$(DEMO_REPO_VOLUME) docker compose \
	-f demo/docker-compose.demo.yml --env-file demo/demo.env
TEST_COMPOSE := docker compose -f tests/docker-compose.test.yml

ssm-bootstrap:
	@# Bind this checkout to the deployment's secrets project/configuration.
	@# The binding is written to a gitignored .ssm/, which is the whole point:
	@# the identifiers are supplied here, at run time, by whoever deploys — so
	@# no project or configuration name is ever committed. Pass them in:
	@#   make ssm-bootstrap SSM_PROJECT=<slug> SSM_CONFIG=<slug>
	@test -n "$(SSM_PROJECT)" || { echo "SSM_PROJECT is required"; exit 1; }
	@test -n "$(SSM_CONFIG)" || { echo "SSM_CONFIG is required"; exit 1; }
	@command -v ssm >/dev/null || { echo "the ssm CLI is not on PATH"; exit 1; }
	ssm setup --project "$(SSM_PROJECT)" --config "$(SSM_CONFIG)" --local-only
	@echo "Bound. 'make redeploy' now resolves secrets without any flags."

redeploy:
	@# Rebuild + redeploy the stack. Secrets are fetched at run time from the
	@# binding above; nothing is read out of the working tree.
	@command -v ssm >/dev/null || { echo "the ssm CLI is not on PATH"; exit 1; }
	@test -f .ssm/config.json || { echo "not bound yet — run 'make ssm-bootstrap' first"; exit 1; }
	bash scripts/deploy/redeploy.sh

bootstrap:
	uv venv $(VENV)
	uv pip install -e .[dev]
	uv pip install -e packages/mewbo_core -e packages/mewbo_tools \
		-e apps/mewbo_api -e apps/mewbo_cli \
		-e apps/mewbo_ha_conversation

lint:
	$(VENV)/bin/ruff check .

lint-fix:
	$(VENV)/bin/ruff check --fix .

typecheck:
	$(VENV)/bin/mypy

precommit-install:
	$(VENV)/bin/pre-commit install

# Tier 2 — the marked tests that need a REAL mongod (query counts, index
# behaviour, benchmarks). A plain `pytest` neither needs nor starts this: the
# tier-2 tests skip when the container is absent. A path is passed on purpose,
# overriding `testpaths` — the tier lives in tests/ and collecting the other
# five suites to deselect all of them costs more than the tier itself takes to
# run. The teardown runs whether pytest passed or failed, and `-v` takes the
# ephemeral database with it, so the next run starts from a blank world.
test-mongo:
	$(TEST_COMPOSE) up -d --wait mongo
	@set +e; $(VENV)/bin/python -m pytest -m realmongo tests/; rc=$$?; \
	  $(TEST_COMPOSE) down -v --remove-orphans; exit $$rc

vendor-aider:
	./scripts/vendor_aider.sh

openapi:
	uv run python scripts/ci/generate_openapi_spec.py

# Serving is deliberately non-strict: mkdocs.yml sets strict, and an in-progress
# link would otherwise abort the first build and exit the server while authoring.
docs:
	uv run --group docs mkdocs serve --no-strict --dev-addr $(DOCS_ADDR)

# Strictness comes from mkdocs.yml so every caller gets it, this one included.
docs-build:
	uv run --group docs mkdocs build

aura-apk:
	cd $(AURA_DIR) && ANDROID_HOME=$(ANDROID_HOME) ./gradlew :app:assembleDebug
	@echo $(CURDIR)/$(AURA_DIR)/app/build/outputs/apk/debug/app-debug.apk

aura-release:
	cd $(AURA_DIR) && ANDROID_HOME=$(ANDROID_HOME) ./gradlew :app:assembleRelease
	@echo $(CURDIR)/$(AURA_DIR)/app/build/outputs/apk/release/app-release.apk

aura-install: aura-apk
	adb -s $(AURA_SERIAL) install -r $(AURA_DIR)/app/build/outputs/apk/debug/app-debug.apk

# Demo-as-code — an ephemeral, isolated stack (own bridge network) for
# scripted screen-capture. A temporary repo volume is staged from this checkout
# so remote Docker daemons see the same files without host-path bind mounts.
# Never touches the root docker-compose.yml stack or its ports/network.
# See demo/CLAUDE.md.
demo-build:
	docker build -f docker/Dockerfile.base -t mewbo-base:demo .
	$(DEMO_COMPOSE) build api console

demo-stage:
	-docker volume rm $(DEMO_REPO_VOLUME)
	docker volume create $(DEMO_REPO_VOLUME)
	tar -C $(CURDIR) -cf - . | docker run --rm -i \
		-v $(DEMO_REPO_VOLUME):/work alpine:3.22 tar -C /work -xf -

demo-sync:
	docker run --rm -v $(DEMO_REPO_VOLUME):/work:ro alpine:3.22 \
		tar -C /work -cf - docs/assets/img-src docs/assets/img | tar -C $(CURDIR) -xf -

demo-up: demo-stage demo-build
	$(DEMO_COMPOSE) up -d --wait mongo api console

demo-seed:
	$(DEMO_COMPOSE) run --rm seed

# shots-cache-init is a profiled one-shot; `compose run` leaves it exited (only
# `shots` is --rm'd), pinning the network it was created on. A later `run`
# restarts it against a now-removed network → "network <id> not found". Clear
# the shots-profile containers first so each capture starts clean.
demo-shots-web:
	-$(DEMO_COMPOSE) rm -fs shots-cache-init shots
	$(DEMO_COMPOSE) run --rm shots

# `down` alone leaves PROFILED (seed/shots) containers behind (they're not
# orphans — they're declared, just inactive), which is what strands a stale
# shots-cache-init across cycles; name the profiles so they're torn down too.
demo-down:
	$(DEMO_COMPOSE) --profile seed --profile shots down -v --remove-orphans
	-docker volume rm $(DEMO_REPO_VOLUME)

# Captures are synced from the temporary repo volume into docs/assets/img-src;
# the docs reference docs/assets/img. This is the transform between them — it
# composites every full-window capture onto a 16:9 wallpaper canvas and copies
# the rest through. Safe to re-run; it reads sources and never its own output.
demo-frame:
	uv run --package mewbo-demo-framer mewbo-demo-frame

demo: demo-up demo-seed demo-shots-web demo-sync demo-frame
