# ALI-JARVIS

Phase 1 provides the stable backend foundation for ALI-JARVIS: a typed FastAPI service with environment-based configuration, centralized logging, and a health endpoint.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
cp .env.example .env
```

## Run

```bash
uv run uvicorn jarvis.main:app --host 127.0.0.1 --port 8000
```

The service exposes `GET /health`.

## Checks

```bash
make check
make typecheck
```

Phase 1 intentionally contains no agent, integrations, memory, UI, voice, automation, or permission system.
