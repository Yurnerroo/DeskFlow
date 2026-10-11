import pytest
from django.db import IntegrityError, transaction

from payments.models import PayoutStatus, Provider
from tests.factories import PayoutFactory

pytestmark = pytest.mark.django_db


def test_payout_defaults_to_created_wise_with_unique_idempotency_key() -> None:
    first = PayoutFactory.create()
    second = PayoutFactory.create()

    assert first.status == PayoutStatus.CREATED
    assert first.provider == Provider.WISE
    assert first.idempotency_key != second.idempotency_key


def test_blank_payout_refs_do_not_collide() -> None:
    PayoutFactory.create(provider_ref="")
    PayoutFactory.create(provider_ref="")


def test_payout_provider_ref_is_unique() -> None:
    PayoutFactory.create(provider_ref="tr_1")

    with pytest.raises(IntegrityError), transaction.atomic():
        PayoutFactory.create(provider_ref="tr_1")


def test_payout_amount_must_be_positive() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        PayoutFactory.create(amount_minor=0)
