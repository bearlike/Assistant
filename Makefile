.PHONY: bootstrap lint lint-fix typecheck precommit-install vendor-aider openapi docs docs-build aura-apk aura-release aura-install

VENV ?= .venv
DOCS_ADDR ?= 0.0.0.0:8000
ANDROID_HOME ?= $(HOME)/android-sdk
AURA_DIR := apps/mewbo_aura
AURA_SERIAL ?= localhost:5555

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
