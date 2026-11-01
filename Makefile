# Common commands. Run `make help` to list them.
# seed/export/doctor need CAREERHUNTER_DATA_DIR set (see docs/SETUP.md).

.DEFAULT_GOAL := help
.PHONY: help setup hooks test cov lint format typecheck check verify-tree seed export doctor

help: ## List the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-11s %s\n", $$1, $$2}'

setup: ## Install Python, dependencies and the git hooks
	uv sync --locked
	$(MAKE) hooks

hooks: ## (Re)install the git hooks, the pre-push guard and push.default=current
	uv run pre-commit install
	git config push.default current
	ln -sf "$(CURDIR)/scripts/pre_push_guard.py" "$$(git rev-parse --git-path hooks)/pre-push"

test: ## Run the test suite (synthetic data only)
	uv run pytest -q

cov: ## Run tests with a coverage report
	uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=85

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-format the code
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type check
	uv run mypy

check: lint typecheck cov ## Everything CI runs, except the full-history secret scan
	uv run pre-commit run --all-files

verify-tree: ## Every hook and gitleaks over every file, tracked and untracked
	uv run python scripts/verify_tree.py

seed: ## Import seed.yaml from your data folder (safe to re-run)
	uv run careerhunter seed

export: ## EI export, e.g. make export CLAIM=2026-27 FROM=2026-09-01 TO=2026-09-30
	@test -n "$(CLAIM)" -a -n "$(FROM)" -a -n "$(TO)" || (echo "usage: make export CLAIM=YYYY-YY FROM=YYYY-MM-DD TO=YYYY-MM-DD"; exit 1)
	uv run careerhunter export --claim $(CLAIM) --from $(FROM) --to $(TO)

doctor: ## Check the setup and report what is missing
	uv run careerhunter doctor
