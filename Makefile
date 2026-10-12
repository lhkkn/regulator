.PHONY: check lint ci

LINT_BIN := $(if $(wildcard .venv/bin/ruff),.venv/bin/,)

check:  ## Run the verify gate (failures are the spec). Needs the workspace + network.
	python3 -m conductor check

lint:  ## Static analysis: ruff (lint + import order + format) + mypy (types).
	$(LINT_BIN)ruff check conductor tests
	$(LINT_BIN)ruff format --check conductor tests
	$(LINT_BIN)mypy conductor

ci: lint  ## What CI runs: lint/types + the self-contained unit suite (no workspace needed).
	python3 -m unittest discover -s tests
