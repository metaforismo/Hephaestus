.PHONY: install test lint example rtl-check clean

install:
	python -m pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	ruff format --check .

example:
	hephaestus compile examples/tiny_weights.json --out build/tiny --module hephaestus_tiny

rtl-check: example
	./scripts/check_rtl.sh build/tiny/hephaestus_tiny.sv hephaestus_tiny

clean:
	rm -rf build .pytest_cache .ruff_cache
