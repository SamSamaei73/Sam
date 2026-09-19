.PHONY: install test lint typecheck run check

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .

typecheck:
	uv run mypy src tests

run:
	uv run python -m sam.server

check: lint test typecheck
