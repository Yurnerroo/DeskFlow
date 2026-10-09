# DeskFlow

Coworking desk & meeting-room booking platform — Django + DRF + PostgreSQL.

## Goals
- Bookings without double-booking (PostgreSQL exclusion constraint on time ranges)
- Memberships/subscriptions with Stripe (test mode, webhooks, idempotency)
- Background jobs (Celery + Redis): reminders, expiring holds
- Live availability over WebSockets (Channels)

## Run locally
```bash
cp .env.example .env
docker compose up -d db redis
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
```

## Tests
```bash
uv run pytest
```

Architecture decisions: [`docs/adr`](docs/adr).
