UV ?= uv

.PHONY: deps install uninstall test

deps:
	$(UV) sync

install:
	$(UV) tool install .

uninstall:
	$(UV) tool uninstall cli-deck

test:
	$(UV) run python -m unittest discover -s tests -v
