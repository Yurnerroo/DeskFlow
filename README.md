# DeskFlow

Coworking desk & meeting-room booking platform — Django + DRF + PostgreSQL.

## Goals
- Bookings without double-booking (PostgreSQL exclusion constraint on time ranges)
- Memberships/subscriptions with Stripe (test mode, webhooks, idempotency)
- Background jobs (Celery + Redis): reminders, expiring holds
- Operator payouts via Wise with daily settlement reconciliation
- Live availability over WebSockets (Channels)

## Run locally
```bash
cp .env.example .env
docker compose up -d db redis
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
```

Background jobs (hold expiry, payment reconciliation, payout polling, daily settlement) run on Celery:
```bash
uv run celery -A config worker -l info
uv run celery -A config beat -l info
# or: docker compose up worker beat
```

## Payouts & settlement
Operators are paid out through Wise. Payouts are created idempotently, polled until final (`poll-payouts`), and a daily job (`settle-daily`) compares what came in with what went out per workspace and currency, logging any mismatch. See [ADR 0003](docs/adr/0003-payouts-and-reconciliation.md).

## Tests
```bash
uv run pytest
```

Architecture decisions: [`docs/adr`](docs/adr).
