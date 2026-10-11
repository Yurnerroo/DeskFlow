import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, ClassVar

import pytest
from django.utils import timezone

from bookings.models import BookingStatus
from bookings.services import BookingCoordinator
from payments.models import Payment, PaymentStatus, Provider, Refund, RefundStatus
from payments.providers.base import (
    BasePaymentProvider,
    ChargeRequest,
    ChargeResult,
    RefundResult,
    WebhookEvent,
)
from payments.services import LATE_WEBHOOK_REASON, apply_webhook_event
from tests.factories import BookingFactory, PaymentFactory

pytestmark = pytest.mark.django_db


class FakeProvider(BasePaymentProvider):
    provider = Provider.STRIPE
    status_map: ClassVar[Mapping[str, PaymentStatus]] = {}

    def __init__(self) -> None:
        self.refund_calls: list[dict[str, Any]] = []

    def create_charge(self, request: ChargeRequest) -> ChargeResult:
        raise NotImplementedError

    def get_charge(self, provider_ref: str) -> ChargeResult:
        raise NotImplementedError

    def refund(
        self, provider_ref: str, *, amount_minor: int, currency: str, idempotency_key: str
    ) -> RefundResult:
        self.refund_calls.append(
            {
                "provider_ref": provider_ref,
                "amount_minor": amount_minor,
                "currency": currency,
                "idempotency_key": idempotency_key,
            }
        )
        return RefundResult(provider_ref="re_1", status=RefundStatus.SUCCEEDED)

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def coordinator() -> BookingCoordinator:
    return BookingCoordinator()


def _event(ref: str, status: PaymentStatus, event_id: str = "evt_1") -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id, event_type="test", provider_ref=ref, status=status, payload={}
    )


def _payment(*, expires_in: timedelta, booking_status: BookingStatus = BookingStatus.PENDING):
    booking = BookingFactory.create(status=booking_status, expires_at=timezone.now() + expires_in)
    return PaymentFactory.create(booking=booking, provider_ref="pi_1", status=PaymentStatus.PENDING)


def test_unknown_payment_is_ignored(provider, coordinator):
    assert apply_webhook_event(_event("pi_missing", PaymentStatus.SUCCEEDED), provider) is None


def test_success_confirms_pending_booking(provider, coordinator):
    payment = _payment(expires_in=timedelta(minutes=10))

    apply_webhook_event(_event("pi_1", PaymentStatus.SUCCEEDED), provider, coordinator)

    payment.refresh_from_db()
    payment.booking.refresh_from_db()
    assert payment.status == PaymentStatus.SUCCEEDED
    assert payment.booking.status == BookingStatus.CONFIRMED
    assert provider.refund_calls == []


def test_success_after_hold_expired_refunds(provider, coordinator):
    payment = _payment(expires_in=-timedelta(minutes=1))

    apply_webhook_event(_event("pi_1", PaymentStatus.SUCCEEDED), provider, coordinator)

    payment.refresh_from_db()
    payment.booking.refresh_from_db()
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.booking.status == BookingStatus.REFUNDED_LATE_WEBHOOK

    refund = Refund.objects.get(payment=payment)
    expected_key = uuid.uuid5(payment.idempotency_key, LATE_WEBHOOK_REASON)
    assert refund.idempotency_key == expected_key
    assert refund.status == RefundStatus.SUCCEEDED
    assert refund.provider_ref == "re_1"
    assert refund.reason == LATE_WEBHOOK_REASON
    assert provider.refund_calls == [
        {
            "provider_ref": "pi_1",
            "amount_minor": payment.amount_minor,
            "currency": payment.currency,
            "idempotency_key": str(expected_key),
        }
    ]


def test_success_for_cancelled_booking_refunds(provider, coordinator):
    payment = _payment(expires_in=timedelta(minutes=10), booking_status=BookingStatus.CANCELLED)

    apply_webhook_event(_event("pi_1", PaymentStatus.SUCCEEDED), provider, coordinator)

    payment.refresh_from_db()
    payment.booking.refresh_from_db()
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.booking.status == BookingStatus.REFUNDED_LATE_WEBHOOK
    assert len(provider.refund_calls) == 1


def test_failure_marks_booking_payment_failed(provider, coordinator):
    payment = _payment(expires_in=timedelta(minutes=10))

    apply_webhook_event(_event("pi_1", PaymentStatus.FAILED), provider, coordinator)

    payment.refresh_from_db()
    payment.booking.refresh_from_db()
    assert payment.status == PaymentStatus.FAILED
    assert payment.booking.status == BookingStatus.PAYMENT_FAILED


def test_replay_on_terminal_payment_is_noop(provider, coordinator):
    payment = _payment(expires_in=-timedelta(minutes=1))
    event = _event("pi_1", PaymentStatus.SUCCEEDED)

    apply_webhook_event(event, provider, coordinator)
    apply_webhook_event(event, provider, coordinator)
    apply_webhook_event(_event("pi_1", PaymentStatus.PENDING, "evt_2"), provider, coordinator)

    payment.refresh_from_db()
    assert payment.status == PaymentStatus.REFUNDED
    assert len(provider.refund_calls) == 1
    assert Refund.objects.filter(payment=payment).count() == 1


def test_non_terminal_event_only_updates_status(provider, coordinator):
    booking = BookingFactory.create(expires_at=timezone.now() + timedelta(minutes=10))
    payment = PaymentFactory.create(
        booking=booking, provider_ref="pi_1", status=PaymentStatus.CREATED
    )

    apply_webhook_event(_event("pi_1", PaymentStatus.PENDING), provider, coordinator)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING
    assert booking.status == BookingStatus.PENDING
    assert not Payment.objects.exclude(pk=payment.pk).exists()
