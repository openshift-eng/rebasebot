IS_CI := false
ifeq ($(VIRTUAL_ENV),/opt/app-root)
    IS_CI := true
endif

SRC_DIRS = rebasebot tests

.PHONY: unittests
unittests: ## Run unit & integration tests
	hack/tests.sh

.PHONY: lint
lint: ## Run lint and format in check mode
	ruff check $(SRC_DIRS)
	ruff format --check $(SRC_DIRS)

.PHONY: lint-fix
lint-fix: ## Fix fixable lint issues and format code
	ruff format $(SRC_DIRS)
	ruff check --fix $(SRC_DIRS)

.PHONY: install
install: ## Install into your user python environment.
	python -m pip install --user .

.PHONY: build
build: ## Create build tarball
	uv build

.PHONY: help
help: ## Display this help screen
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: clean
clean:
	rm -r dist/ build/ .pytest_cache/ .mypy_cache rebasebot.egg-info .coverage

.venv: ## Create venv
ifeq ($(IS_CI),false)
	uv venv
endif

# Install dependencies into venv
.PHONY: deps
deps: .venv
ifeq ($(IS_CI),false)
	uv sync --extra dev
else
    # In CI we already are inside a venv
	uv sync --active --extra dev
endif
