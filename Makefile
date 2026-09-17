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
	uv run uvicorn jarvis.main:app --host $${API_HOST:-127.0.0.1} --port $${API_PORT:-8000}

check: lint test
