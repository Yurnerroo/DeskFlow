import datetime as dt
import logging

from celery import shared_task
from django.db import OperationalError

from payments.payouts import poll_payouts
from payments.services import reconcile_pending
from payments.settlement import settle_day

logger = logging.getLogger(__name__)


@shared_task(
    acks_late=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def reconcile_pending_payments() -> int:
    # Safety net for lost webhooks: polls the provider for payments stuck in PENDING/UNKNOWN.
    resolved = reconcile_pending()
    logger.info("reconciled pending payments", extra={"count": resolved})
    return resolved


@shared_task(
    acks_late=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def poll_pending_payouts() -> int:
    # Wise has no webhook wired here, so polling is the primary way payouts reach a final state.
    settled = poll_payouts()
    logger.info("polled payouts", extra={"count": settled})
    return settled


@shared_task(
    acks_late=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def settle_daily_payouts() -> int:
    # Settles yesterday, not today: today's day is still open and would always look short.
    day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    mismatches = settle_day(day)
    logger.info("settled day", extra={"day": day.isoformat(), "count": len(mismatches)})
    return len(mismatches)
