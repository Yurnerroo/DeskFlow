"""Domain side effects of provider payment events (webhooks and reconciliation).

Every path that learns a new payment status from a provider funnels through
``apply_webhook_event`` so that webhooks and the reconciliation poller can never disagree.
"""

import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from bookings.models import BookingStatus
from bookings.services import BookingCoordinator, HoldExpired
from payments.models import Payment, PaymentStatus, Refund
from payments.providers.base import BasePaymentProvider, ProviderError, WebhookEvent
from payments.providers.registry import get_provider

logger = logging.getLogger(__name__)

TERMINAL_PAYMENT_STATUSES = frozenset(
    {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED, PaymentStatus.REFUNDED}
)
LATE_WEBHOOK_REASON = "late_webhook"


def apply_webhook_event(
    event: WebhookEvent,
    provider: BasePaymentProvider,
    coordinator: BookingCoordinator | None = None,
) -> Payment | None:
    """Apply a provider status update to the payment and its booking.

    Idempotent: terminal payment statuses are never regressed, so replays and
    out-of-order deliveries (e.g. ``pending`` after ``succeeded``) are no-ops.
    """
    coordinator = coordinator or BookingCoordinator()
    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .select_related("booking")
            .filter(provider=provider.provider, provider_ref=event.provider_ref)
            .first()
        )
        if payment is None:
            logger.warning(
                "Webhook for unknown payment provider=%s ref=%s event=%s",
                provider.provider,
                event.provider_ref,
                event.event_id,
            )
            return None

        if payment.status in TERMINAL_PAYMENT_STATUSES:
            return payment

        if event.status == PaymentStatus.SUCCEEDED:
            _set_payment_status(payment, PaymentStatus.SUCCEEDED)
            _confirm_or_refund(payment, provider, coordinator)
        elif event.status == PaymentStatus.FAILED:
            _set_payment_status(payment, PaymentStatus.FAILED)
            if payment.booking.status == BookingStatus.PENDING:
                coordinator.fail_payment(payment.booking_id)
        elif event.status != payment.status:
            _set_payment_status(payment, event.status)
        return payment


RECONCILE_AFTER = timedelta(minutes=15)
RESOLVED_BY_PROVIDER = frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED})


def reconcile_pending(
    older_than: timedelta = RECONCILE_AFTER,
    coordinator: BookingCoordinator | None = None,
) -> int:
    """Poll providers for payments whose webhook never arrived (or was lost).

    Only payments stuck in PENDING/UNKNOWN for longer than ``older_than`` are checked, so the
    poller does not race the normal webhook path. A resolved status is fed through
    ``apply_webhook_event`` -- the same code a webhook would run -- which keeps both paths
    idempotent and consistent. One provider outage never blocks the rest of the batch.

    Returns the number of payments that reached a terminal status.
    """
    cutoff = timezone.now() - older_than
    stale = (
        Payment.objects.filter(
            status__in=[PaymentStatus.PENDING, PaymentStatus.UNKNOWN],
            created_at__lt=cutoff,
        )
        .exclude(provider_ref="")
        .order_by("created_at")
    )

    resolved = 0
    for payment in stale.iterator():
        try:
            provider = get_provider(payment.provider)
            result = provider.get_charge(payment.provider_ref)
        except (ProviderError, LookupError) as exc:
            logger.warning("Reconciliation skipped payment %s: %s", payment.pk, exc)
            continue

        if result.status not in RESOLVED_BY_PROVIDER:
            continue

        event = WebhookEvent(
            event_id=f"reconcile:{payment.pk}:{result.status}",
            event_type="reconcile",
            provider_ref=payment.provider_ref,
            status=result.status,
            payload={},
        )
        apply_webhook_event(event, provider, coordinator)
        resolved += 1
        logger.info("Reconciled payment %s to %s", payment.pk, result.status)
    return resolved


def _confirm_or_refund(
    payment: Payment, provider: BasePaymentProvider, coordinator: BookingCoordinator
) -> None:
    booking = payment.booking
    if booking.status == BookingStatus.PENDING:
        try:
            coordinator.confirm(booking.pk)
            return
        except HoldExpired:
            # confirm() persisted EXPIRED in its own savepoint; fall through to refund.
            pass
    elif booking.status == BookingStatus.CONFIRMED:
        return
    elif booking.status not in (BookingStatus.EXPIRED, BookingStatus.CANCELLED):
        logger.error("Payment %s succeeded for booking in %s", payment.pk, booking.status)
        return

    _refund_late_payment(payment, provider)
    coordinator.mark_refunded_late(booking.pk)


def _refund_late_payment(payment: Payment, provider: BasePaymentProvider) -> Refund:
    """The money arrived after the hold was released: give it back.

    Runs inside the caller's transaction. If anything fails the whole event rolls back
    and the provider redelivers. The idempotency key is derived from the payment, so if
    the provider refunded but our commit failed, the retry replays the same refund
    instead of issuing a second one.
    """
    refund = Refund.objects.create(
        payment=payment,
        amount_minor=payment.amount_minor,
        reason=LATE_WEBHOOK_REASON,
        idempotency_key=uuid.uuid5(payment.idempotency_key, LATE_WEBHOOK_REASON),
    )
    result = provider.refund(
        payment.provider_ref,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        idempotency_key=str(refund.idempotency_key),
    )
    refund.status = result.status
    refund.provider_ref = result.provider_ref
    refund.save(update_fields=["status", "provider_ref", "updated_at"])
    _set_payment_status(payment, PaymentStatus.REFUNDED)
    logger.info("Refunded late payment %s (refund %s)", payment.pk, refund.pk)
    return refund


def _set_payment_status(payment: Payment, status: PaymentStatus) -> None:
    payment.status = status
    payment.save(update_fields=["status", "updated_at"])
