.PHONY: check fixture lint test typecheck validate

PYTHON ?= python3

check: lint typecheck test validate

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

typecheck:
	$(PYTHON) -m mypy

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

validate:
	PYTHONPATH=src $(PYTHON) -m market_radar validate examples/snapshot.v1.json

fixture:
	PYTHONPATH=src $(PYTHON) -m market_radar canonicalize examples/snapshot.v1.json
