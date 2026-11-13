.PHONY: setup test lint demo
setup:
	uv sync --locked
test:
	uv run --locked pytest
lint:
	uv run --locked ruff check src tests
demo:
	uv run --locked python tests/demo.py --work-dir .work/demo --output-dir .work/demo-export
