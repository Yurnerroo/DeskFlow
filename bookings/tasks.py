import logging

from celery import shared_task
from django.db import OperationalError

from bookings.services import BookingCoordinator

logger = logging.getLogger(__name__)


@shared_task(
    acks_late=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def expire_stale_holds() -> int:
    # Idempotent: a single conditional UPDATE, safe if two workers race.
    expired = BookingCoordinator().expire_stale()
    logger.info("expired stale holds", extra={"count": expired})
    return expired
