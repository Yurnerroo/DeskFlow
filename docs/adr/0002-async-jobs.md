# 2. Async jobs with Celery + beat

Date: 2026-10-09
Status: Accepted

## Context
Two pieces of work must happen outside the request cycle:
- **Hold expiry** — a booking held for payment (10-minute TTL) must be released if the
  customer never pays, otherwise the slot stays blocked.
- **Payment reconciliation** — webhooks can be lost or delayed; payments stuck in `pending`
  must be re-checked against the provider so bookings don't hang forever.

## Decision
- **Celery** with **Redis** as broker; `config/celery.py` autodiscovers `tasks.py` per app.
- Tasks are idempotent sweeps, so running twice or on several workers is safe: hold expiry is
  a single conditional `UPDATE ... WHERE status = 'pending' AND expires_at <= now()`, and
  reconciliation only touches payments still `pending`/`unknown`, feeding results through the
  same idempotent path as webhooks.
- `acks_late=True` + retry with backoff/jitter on `OperationalError` — a crashed worker or
  transient DB blip re-delivers the task instead of losing it.
- **Celery beat** schedules the sweeps (`CELERY_BEAT_SCHEDULE`): holds every 60 s,
  reconciliation every 300 s, both overridable via env. Each entry sets `expires` just under
  its interval so a backlog never piles up duplicate sweeps.
- `worker` and `beat` run as separate Compose services from the same image.

## Consequences
- Exactly one beat process must run (duplicate beats double-schedule; harmless thanks to
  idempotency, but wasteful).
- Hold expiry is eventually consistent: an overdue hold keeps blocking its slot (the
  exclusion constraint covers `pending`) for up to TTL + 60 s until the sweep runs. Payment
  can never sneak through late — `confirm()` re-checks `expires_at` and moves the booking to
  `expired` instead.
- Tests call tasks directly or via `.apply()` (in-process); no broker or worker needed in CI.
