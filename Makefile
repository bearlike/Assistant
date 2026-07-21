.PHONY: bootstrap lint lint-fix typecheck precommit-install vendor-aider openapi docs docs-build aura-apk aura-release aura-install demo-build demo-up demo-seed demo-shots-web demo-down demo

VENV ?= .venv
DOCS_ADDR ?= 0.0.0.0:8000
ANDROID_HOME ?= $(HOME)/android-sdk
AURA_DIR := apps/mewbo_aura
AURA_SERIAL ?= localhost:5555
DEMO_COMPOSE := docker compose -f demo/docker-compose.demo.yml --env-file demo/demo.env

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

vendor-aider:
	./scripts/vendor_aider.sh

openapi:
	uv run python scripts/ci/generate_openapi_spec.py

docs:
	uv run --group docs mkdocs serve --dev-addr $(DOCS_ADDR)

docs-build:
	uv run --group docs mkdocs build --strict

aura-apk:
	cd $(AURA_DIR) && ANDROID_HOME=$(ANDROID_HOME) ./gradlew :app:assembleDebug
	@echo $(CURDIR)/$(AURA_DIR)/app/build/outputs/apk/debug/app-debug.apk

aura-release:
	cd $(AURA_DIR) && ANDROID_HOME=$(ANDROID_HOME) ./gradlew :app:assembleRelease
	@echo $(CURDIR)/$(AURA_DIR)/app/build/outputs/apk/release/app-release.apk

aura-install: aura-apk
	adb -s $(AURA_SERIAL) install -r $(AURA_DIR)/app/build/outputs/apk/debug/app-debug.apk

# Demo-as-code — an ephemeral, isolated stack (own bridge
# network, no data volumes) for scripted screen-capture. Never touches the
# root docker-compose.yml stack or its ports/network. See demo/CLAUDE.md.
demo-build:
	$(DEMO_COMPOSE) build

demo-up:
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

demo: demo-up demo-seed demo-shots-web
