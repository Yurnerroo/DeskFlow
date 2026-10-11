import logging

from celery import shared_task
from django.db import OperationalError

from payments.services import reconcile_pending

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
