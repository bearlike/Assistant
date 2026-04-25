.PHONY: install dev build lint lint-ci typecheck test test-ci test-e2e clean

install:          ## Install dependencies
	npm install

dev:              ## Start development server
	npm run dev

build:            ## Production build
	npm run build

lint:             ## Run ESLint
	npm run lint

lint-ci:          ## Run ESLint in CI mode (fail on warnings)
	npm run lint:ci

typecheck:        ## Run TypeScript type checking
	npm run typecheck

test:             ## Run unit tests
	npm test

test-ci:          ## Run tests in single-run mode
	npm run test:ci

test-e2e:         ## Run end-to-end tests
	npm run test:e2e

clean:            ## Remove build artifacts
	rm -rf dist node_modules
