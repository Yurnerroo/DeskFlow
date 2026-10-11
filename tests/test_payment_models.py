import pytest
from django.db import IntegrityError, transaction

from payments.models import PaymentStatus, ProcessedWebhookEvent, Provider
from tests.factories import PaymentFactory

pytestmark = pytest.mark.django_db


def test_payment_defaults_to_created_with_unique_idempotency_key() -> None:
    first = PaymentFactory.create()
    second = PaymentFactory.create()

    assert first.status == PaymentStatus.CREATED
    assert first.idempotency_key != second.idempotency_key


def test_blank_provider_refs_do_not_collide() -> None:
    PaymentFactory.create(provider_ref="")
    PaymentFactory.create(provider_ref="")


def test_provider_ref_is_unique_per_provider() -> None:
    PaymentFactory.create(provider=Provider.STRIPE, provider_ref="pi_123")
    PaymentFactory.create(provider=Provider.PAYPAL, provider_ref="pi_123")

    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentFactory.create(provider=Provider.STRIPE, provider_ref="pi_123")


def test_amount_must_be_positive() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentFactory.create(amount_minor=0)


def test_webhook_event_is_unique_per_provider() -> None:
    ProcessedWebhookEvent.objects.create(provider=Provider.STRIPE, event_id="evt_1", event_type="x")
    ProcessedWebhookEvent.objects.create(provider=Provider.PAYPAL, event_id="evt_1", event_type="x")

    with pytest.raises(IntegrityError), transaction.atomic():
        ProcessedWebhookEvent.objects.create(
            provider=Provider.STRIPE, event_id="evt_1", event_type="x"
        )
