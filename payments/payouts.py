"""Payout service: money going out to payees via a payout rail (Wise).

Kept apart from ``services.py`` because the lifecycle is different: a payout has no
booking state machine to drive, settles over hours or days, and is only ever
confirmed by polling the provider.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from payments.models import Payee, Payout, PayoutStatus
from payments.providers.base import PayoutRequest, ProviderError
from payments.providers.registry import get_payout_provider

if TYPE_CHECKING:
    from bookings.models import Booking

logger = logging.getLogger(__name__)

_FAILURE_REASON_MAX = 255
_TERMINAL = frozenset({PayoutStatus.PAID, PayoutStatus.FAILED})


def create_payout(
    payee: Payee,
    amount_minor: int,
    *,
    currency: str | None = None,
    booking: Booking | None = None,
    reference: str = "",
) -> Payout:
    """Persist the payout first, then call the provider with its idempotency key.

    The row exists before any money moves, so a crash between the HTTP call and the
    save is recoverable: retrying sends the same key and Wise returns the same transfer.
    """
    payout = Payout.objects.create(
        payee=payee,
        booking=booking,
        amount_minor=amount_minor,
        currency=(currency or payee.currency).upper(),
    )
    request = PayoutRequest(
        amount_minor=payout.amount_minor,
        currency=payout.currency,
        idempotency_key=str(payout.idempotency_key),
        recipient_id=payee.wise_recipient_id,
        reference=reference,
    )
    try:
        result = get_payout_provider(payout.provider).create_payout(request)
    except ProviderError as exc:
        if exc.retryable:
            # Left in CREATED: the caller retries with the same idempotency key.
            raise
        payout.status = PayoutStatus.FAILED
        payout.failure_reason = str(exc)[:_FAILURE_REASON_MAX]
        payout.save(update_fields=["status", "failure_reason", "updated_at"])
        return payout

    payout.provider_ref = result.provider_ref
    payout.status = result.status
    payout.failure_reason = result.failure_reason[:_FAILURE_REASON_MAX]
    payout.save(update_fields=["provider_ref", "status", "failure_reason", "updated_at"])
    return payout


def poll_payouts() -> int:
    """Ask the provider about every in-flight payout; return how many became terminal."""
    in_flight = (
        Payout.objects.filter(status__in=[PayoutStatus.PROCESSING, PayoutStatus.UNKNOWN])
        .exclude(provider_ref="")
        .order_by("created_at")
    )
    settled = 0
    for payout in in_flight.iterator():
        try:
            result = get_payout_provider(payout.provider).get_payout(payout.provider_ref)
        except (ProviderError, LookupError) as exc:
            # One broken payout must not block the rest; the next run retries it.
            logger.warning("payout poll failed", extra={"payout_id": payout.pk, "error": str(exc)})
            continue
        if _apply_result(payout.pk, result.status, result.failure_reason):
            settled += 1
    return settled


def _apply_result(payout_id: int, status: PayoutStatus, failure_reason: str) -> bool:
    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(pk=payout_id)
        # A concurrent poll may have already settled it; terminal states are final.
        if payout.status in _TERMINAL or payout.status == status:
            return False
        payout.status = status
        payout.failure_reason = failure_reason[:_FAILURE_REASON_MAX]
        payout.save(update_fields=["status", "failure_reason", "updated_at"])
    return status in _TERMINAL
