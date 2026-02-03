.PHONY: lint lint-fix typecheck precommit-install

lint:
	poetry run ruff check .

lint-fix:
	poetry run ruff check --fix .

typecheck:
	poetry run mypy
	cd meeseeks-api && poetry run mypy
	cd meeseeks-chat && poetry run mypy
	cd meeseeks-cli && poetry run mypy

precommit-install:
	poetry run pre-commit install
