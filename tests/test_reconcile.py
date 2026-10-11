from collections.abc import Mapping
from datetime import timedelta
from typing import ClassVar

import pytest
from django.utils import timezone

from bookings.models import BookingStatus
from payments.models import Payment, PaymentStatus, Provider, RefundStatus
from payments.providers import registry
from payments.providers.base import (
    BasePaymentProvider,
    ChargeRequest,
    ChargeResult,
    ProviderError,
    RefundResult,
    WebhookEvent,
)
from payments.services import reconcile_pending
from tests.factories import BookingFactory, PaymentFactory

pytestmark = pytest.mark.django_db


class PollingProvider(BasePaymentProvider):
    provider = Provider.STRIPE
    status_map: ClassVar[Mapping[str, PaymentStatus]] = {}

    def __init__(self) -> None:
        self.statuses: dict[str, PaymentStatus] = {}
        self.broken: set[str] = set()
        self.polled: list[str] = []

    def create_charge(self, request: ChargeRequest) -> ChargeResult:
        raise NotImplementedError

    def get_charge(self, provider_ref: str) -> ChargeResult:
        self.polled.append(provider_ref)
        if provider_ref in self.broken:
            raise ProviderError("provider timeout", retryable=True)
        return ChargeResult(provider_ref=provider_ref, status=self.statuses[provider_ref])

    def refund(
        self, provider_ref: str, *, amount_minor: int, currency: str, idempotency_key: str
    ) -> RefundResult:
        return RefundResult(provider_ref="re_1", status=RefundStatus.SUCCEEDED)

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> PollingProvider:
    instance = PollingProvider()
    monkeypatch.setitem(registry._registry, Provider.STRIPE, lambda: instance)
    return instance


def _stale_payment(ref: str, status: PaymentStatus = PaymentStatus.PENDING) -> Payment:
    booking = BookingFactory.create(
        status=BookingStatus.PENDING, expires_at=timezone.now() + timedelta(minutes=10)
    )
    payment = PaymentFactory.create(booking=booking, provider_ref=ref, status=status)
    Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(minutes=30))
    payment.refresh_from_db()
    return payment


def test_succeeded_at_provider_confirms_booking(provider: PollingProvider) -> None:
    payment = _stale_payment("pi_ok")
    provider.statuses["pi_ok"] = PaymentStatus.SUCCEEDED

    assert reconcile_pending() == 1

    payment.refresh_from_db()
    payment.booking.refresh_from_db()
    assert payment.status == PaymentStatus.SUCCEEDED
    assert payment.booking.status == BookingStatus.CONFIRMED


def test_unknown_status_is_resolved_too(provider: PollingProvider) -> None:
    payment = _stale_payment("pi_unknown", status=PaymentStatus.UNKNOWN)
    provider.statuses["pi_unknown"] = PaymentStatus.SUCCEEDED

    assert reconcile_pending() == 1

    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SUCCEEDED


def test_failed_at_provider_fails_booking(provider: PollingProvider) -> None:
    payment = _stale_payment("pi_fail")
    provider.statuses["pi_fail"] = PaymentStatus.FAILED

    assert reconcile_pending() == 1

    payment.refresh_from_db()
    payment.booking.refresh_from_db()
    assert payment.status == PaymentStatus.FAILED
    assert payment.booking.status == BookingStatus.PAYMENT_FAILED


def test_still_pending_at_provider_is_left_alone(provider: PollingProvider) -> None:
    payment = _stale_payment("pi_wait")
    provider.statuses["pi_wait"] = PaymentStatus.PENDING

    assert reconcile_pending() == 0

    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING


def test_fresh_payments_are_not_polled(provider: PollingProvider) -> None:
    booking = BookingFactory.create(
        status=BookingStatus.PENDING, expires_at=timezone.now() + timedelta(minutes=10)
    )
    PaymentFactory.create(booking=booking, provider_ref="pi_new", status=PaymentStatus.PENDING)

    assert reconcile_pending() == 0
    assert provider.polled == []


def test_terminal_and_refless_payments_are_skipped(provider: PollingProvider) -> None:
    _stale_payment("pi_done", status=PaymentStatus.SUCCEEDED)
    _stale_payment("")

    assert reconcile_pending() == 0
    assert provider.polled == []


def test_provider_error_does_not_stop_the_batch(provider: PollingProvider) -> None:
    _stale_payment("pi_broken")
    healthy = _stale_payment("pi_healthy")
    provider.broken.add("pi_broken")
    provider.statuses["pi_healthy"] = PaymentStatus.SUCCEEDED

    assert reconcile_pending() == 1

    healthy.refresh_from_db()
    assert healthy.status == PaymentStatus.SUCCEEDED
    assert set(provider.polled) == {"pi_broken", "pi_healthy"}


def test_reconcile_is_idempotent(provider: PollingProvider) -> None:
    _stale_payment("pi_twice")
    provider.statuses["pi_twice"] = PaymentStatus.SUCCEEDED

    assert reconcile_pending() == 1
    assert reconcile_pending() == 0
