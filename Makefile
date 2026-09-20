.PHONY: install test lint typecheck run check desktop-install desktop-check desktop-rust-check desktop-build

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

# Desktop UI (Phase 11). Deliberately separate from `check`, which stays Python-only.
desktop-install:
	cd desktop && npm ci

desktop-check:
	cd desktop && npm run lint && npm run typecheck && npm test && npm run build

desktop-rust-check:
	cd desktop/src-tauri && cargo fmt --check && cargo check && cargo test && cargo clippy --all-targets -- -D warnings

desktop-build:
	cd desktop && npm run tauri build
