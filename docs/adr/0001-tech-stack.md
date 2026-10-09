# 1. Tech stack and local environment

Date: 2026-10-09
Status: Accepted

## Context
DeskFlow is a booking platform where correctness under concurrency (no double-booking),
payments and async jobs matter more than UI. The stack should mirror production backend work.

## Decision
- **Django + DRF** for the API; **drf-spectacular** for OpenAPI docs.
- **PostgreSQL 16** — needed for `tstzrange` + exclusion constraints (btree_gist).
- **Redis** — Celery broker and Channels layer later.
- **uv** for dependency management (`package = false`, app not a library).
- **django-environ** — 12-factor config from env vars / `.env`.
- **Docker Compose** for db/redis; app runs locally via uv or in the `web` container.
- **pytest-django + factory_boy** for tests, **ruff** for lint/format.

## Consequences
- SQLite is not supported (exclusion constraints are Postgres-only) — tests need Postgres.
- One `.env` drives both local and container runs; secrets never committed.
