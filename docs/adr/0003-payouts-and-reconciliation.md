# 3. Payouts and settlement reconciliation

Date: 2026-10-09

Status: Accepted

## Context
- **Money flows both ways.** Customers pay in via Stripe/PayPal; space operators must be paid out. Payouts go through Wise, which settles over hours or days, not seconds.
- **No reliable push signal.** Wise transfer state is confirmed by polling the transfer status; we cannot assume a webhook arrives for every transition.
- **Flows drift.** Payments, refunds and payouts are recorded by independent code paths and providers. Without a cross-check, a missed payout or a double payout is only found when an operator complains.

## Decision
- **Separate module.** Payouts live in `payments/payouts.py`, not `services.py`: they have no booking state machine and a different lifecycle (`CREATED → PROCESSING → PAID | FAILED`, plus `UNKNOWN`).
- **Persist, then call.** `create_payout` writes the `Payout` row first and sends its `idempotency_key` to Wise. If the process dies after the HTTP call, a retry returns the same transfer instead of sending money twice.
- **Error classes.** A retryable provider error leaves the payout `CREATED` and re-raises so Celery retries; a non-retryable one marks it `FAILED`. A status the client cannot map becomes `UNKNOWN` and keeps being polled instead of guessing.
- **Polling.** `poll_pending_payouts` (beat `poll-payouts`, every 5 min, `POLL_PAYOUTS_EVERY_SECONDS`) checks `PROCESSING`/`UNKNOWN` payouts that have a `provider_ref`. One payout's failure is logged and does not stop the batch.
- **Row locks, final terminal states.** Status updates take `select_for_update`; `PAID` and `FAILED` are never overwritten, so a late or out-of-order poll result is harmless.
- **Provider registry.** `get_payout_provider` resolves the client by name, the same pattern as pay-in providers, so a second rail is a new class, not a rewrite.
- **Daily settlement.** `settle_daily_payouts` (beat `settle-daily`, `SETTLEMENT_EVERY_SECONDS`) runs `settle_day` for yesterday's UTC window, per workspace and currency:
  - expected = succeeded/refunded payments − succeeded refunds;
  - actual = payouts in `CREATED`, `PROCESSING`, `UNKNOWN` or `PAID` — in-flight transfers count, so slow rails don't raise false alarms;
  - workspaces are filtered via a subquery on payees, not a join, so several payees per workspace don't multiply sums.
- **Report, don't fix.** Mismatches are returned and logged as `settlement mismatch`. The job never moves money on its own.

## Consequences
- **Latency.** A payout is seen as `PAID` up to one poll interval after Wise settles it. Acceptable for payouts; not for pay-ins, which keep webhooks.
- **Day boundary.** A payout created after midnight for the previous day's earnings shows as a mismatch for that day. Fix later by linking payouts to a settlement period rather than to `created_at`.
- **Single rail.** Only Wise is implemented; multi-currency routing and FX are out of scope.
- **Human in the loop.** Report-only means someone has to act on mismatches — the safe default when the cost of an automatic wrong correction is real money.
