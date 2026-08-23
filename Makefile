.PHONY: test validate fixture

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

validate:
	PYTHONPATH=src python3 -m market_radar validate examples/snapshot.v1.json

fixture:
	PYTHONPATH=src python3 -m market_radar canonicalize examples/snapshot.v1.json

