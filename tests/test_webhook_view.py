from collections.abc import Mapping
from datetime import timedelta
from typing import ClassVar

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import BookingStatus
from payments.models import PaymentStatus, ProcessedWebhookEvent, Provider
from payments.providers import ProviderError, WebhookVerificationError, registry
from payments.providers.base import (
    BasePaymentProvider,
    ChargeRequest,
    ChargeResult,
    RefundResult,
    WebhookEvent,
)
from tests.factories import BookingFactory, PaymentFactory

pytestmark = pytest.mark.django_db


class WebhookFakeProvider(BasePaymentProvider):
    provider = Provider.STRIPE
    status_map: ClassVar[Mapping[str, PaymentStatus]] = {}

    def __init__(self) -> None:
        self.event: WebhookEvent | None = None
        self.parse_error: Exception | None = None
        self.refund_error: Exception | None = None

    def create_charge(self, request: ChargeRequest) -> ChargeResult:
        raise NotImplementedError

    def get_charge(self, provider_ref: str) -> ChargeResult:
        raise NotImplementedError

    def refund(
        self, provider_ref: str, *, amount_minor: int, currency: str, idempotency_key: str
    ) -> RefundResult:
        if self.refund_error is not None:
            raise self.refund_error
        raise NotImplementedError

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        if self.parse_error is not None:
            raise self.parse_error
        assert self.event is not None
        return self.event


@pytest.fixture
def fake(monkeypatch) -> WebhookFakeProvider:
    instance = WebhookFakeProvider()
    monkeypatch.setitem(registry._registry, Provider.STRIPE, lambda: instance)
    return instance


@pytest.fixture
def client() -> APIClient:
    return APIClient()


def _url(provider: str = "stripe") -> str:
    return reverse("provider-webhook", kwargs={"provider": provider})


def _pending_payment(*, expires_in: timedelta = timedelta(minutes=10)):
    booking = BookingFactory.create(
        status=BookingStatus.PENDING, expires_at=timezone.now() + expires_in
    )
    return PaymentFactory.create(booking=booking, provider_ref="pi_1", status=PaymentStatus.PENDING)


def _event(status: PaymentStatus, event_id: str = "evt_1") -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id,
        event_type="payment_intent.test",
        provider_ref="pi_1",
        status=status,
        payload={"id": event_id},
    )


def test_unknown_provider_returns_404(client):
    response = client.post(_url("bitcoin"), data=b"{}", content_type="application/json")

    assert response.status_code == 404


def test_invalid_signature_returns_400_and_stores_nothing(client, fake):
    fake.parse_error = WebhookVerificationError("bad signature")

    response = client.post(_url(), data=b"{}", content_type="application/json")

    assert response.status_code == 400
    assert not ProcessedWebhookEvent.objects.exists()


def test_success_event_confirms_booking_and_records_inbox(client, fake):
    payment = _pending_payment()
    fake.event = _event(PaymentStatus.SUCCEEDED)

    response = client.post(_url(), data=b"{}", content_type="application/json")

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    payment.refresh_from_db()
    payment.booking.refresh_from_db()
    assert payment.status == PaymentStatus.SUCCEEDED
    assert payment.booking.status == BookingStatus.CONFIRMED
    inbox = ProcessedWebhookEvent.objects.get()
    assert inbox.provider == Provider.STRIPE
    assert inbox.event_id == "evt_1"
    assert inbox.payload == {"id": "evt_1"}
    assert inbox.processed_at is not None


def test_provider_slug_is_case_insensitive(client, fake):
    _pending_payment()
    fake.event = _event(PaymentStatus.SUCCEEDED)

    response = client.post(_url("STRIPE"), data=b"{}", content_type="application/json")

    assert response.status_code == 200


def test_duplicate_event_is_acknowledged_without_side_effects(client, fake):
    payment = _pending_payment()
    fake.event = _event(PaymentStatus.SUCCEEDED)
    client.post(_url(), data=b"{}", content_type="application/json")

    # Same event id redelivered, even with a different status, must be ignored.
    fake.event = _event(PaymentStatus.FAILED)
    response = client.post(_url(), data=b"{}", content_type="application/json")

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate"}
    assert ProcessedWebhookEvent.objects.count() == 1
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SUCCEEDED


def test_provider_failure_rolls_back_inbox_so_redelivery_is_processed(client, fake):
    payment = _pending_payment(expires_in=timedelta(minutes=-1))
    payment.booking.status = BookingStatus.EXPIRED
    payment.booking.save(update_fields=["status"])
    fake.event = _event(PaymentStatus.SUCCEEDED)
    fake.refund_error = ProviderError("gateway down", retryable=True)

    response = client.post(_url(), data=b"{}", content_type="application/json")

    assert response.status_code == 503
    assert not ProcessedWebhookEvent.objects.exists()
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING
