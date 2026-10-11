from collections.abc import Mapping
from typing import ClassVar

import pytest

from payments.models import Payout, PayoutStatus, Provider
from payments.payouts import create_payout, poll_payouts
from payments.providers import registry
from payments.providers.base import (
    BasePayoutProvider,
    PayoutRequest,
    PayoutResult,
    ProviderError,
)
from tests.factories import PayeeFactory, PayoutFactory

pytestmark = pytest.mark.django_db


class FakePayoutProvider(BasePayoutProvider):
    provider = Provider.WISE
    status_map: ClassVar[Mapping[str, PayoutStatus]] = {}

    def __init__(self) -> None:
        self.requests: list[PayoutRequest] = []
        self.create_error: ProviderError | None = None
        self.create_status = PayoutStatus.PROCESSING
        self.statuses: dict[str, PayoutStatus] = {}
        self.broken: set[str] = set()
        self.polled: list[str] = []

    def create_payout(self, request: PayoutRequest) -> PayoutResult:
        self.requests.append(request)
        if self.create_error is not None:
            raise self.create_error
        return PayoutResult(provider_ref="tr_1", status=self.create_status)

    def get_payout(self, provider_ref: str) -> PayoutResult:
        self.polled.append(provider_ref)
        if provider_ref in self.broken:
            raise ProviderError("provider timeout", retryable=True)
        status = self.statuses[provider_ref]
        reason = "account closed" if status == PayoutStatus.FAILED else ""
        return PayoutResult(provider_ref=provider_ref, status=status, failure_reason=reason)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakePayoutProvider:
    instance = FakePayoutProvider()
    monkeypatch.setitem(registry._payout_registry, Provider.WISE, lambda: instance)
    return instance


# -- create ---------------------------------------------------------------------------


def test_create_payout_saves_ref_and_status(provider: FakePayoutProvider) -> None:
    payee = PayeeFactory.create(currency="eur")

    payout = create_payout(payee, 5000, reference="May settlement")

    payout.refresh_from_db()
    assert payout.provider_ref == "tr_1"
    assert payout.status == PayoutStatus.PROCESSING
    assert payout.currency == "EUR"
    [request] = provider.requests
    assert request.recipient_id == payee.wise_recipient_id
    assert request.idempotency_key == str(payout.idempotency_key)
    assert request.amount_minor == 5000
    assert request.reference == "May settlement"


def test_create_payout_marks_failed_on_permanent_error(provider: FakePayoutProvider) -> None:
    provider.create_error = ProviderError("x" * 400)

    payout = create_payout(PayeeFactory.create(), 5000)

    payout.refresh_from_db()
    assert payout.status == PayoutStatus.FAILED
    assert len(payout.failure_reason) == 255
    assert payout.provider_ref == ""


def test_create_payout_reraises_retryable_error_and_keeps_row(
    provider: FakePayoutProvider,
) -> None:
    provider.create_error = ProviderError("timeout", retryable=True)

    with pytest.raises(ProviderError):
        create_payout(PayeeFactory.create(), 5000)

    payout = Payout.objects.get()
    assert payout.status == PayoutStatus.CREATED
    assert payout.failure_reason == ""


# -- poll -----------------------------------------------------------------------------


def _in_flight(ref: str, status: PayoutStatus = PayoutStatus.PROCESSING) -> Payout:
    return PayoutFactory.create(provider_ref=ref, status=status)


def test_poll_settles_paid_and_failed(provider: FakePayoutProvider) -> None:
    paid = _in_flight("tr_paid")
    failed = _in_flight("tr_failed", PayoutStatus.UNKNOWN)
    provider.statuses = {"tr_paid": PayoutStatus.PAID, "tr_failed": PayoutStatus.FAILED}

    assert poll_payouts() == 2

    paid.refresh_from_db()
    failed.refresh_from_db()
    assert paid.status == PayoutStatus.PAID
    assert failed.status == PayoutStatus.FAILED
    assert failed.failure_reason == "account closed"


def test_poll_keeps_unknown_and_does_not_count_it(provider: FakePayoutProvider) -> None:
    payout = _in_flight("tr_1")
    provider.statuses = {"tr_1": PayoutStatus.UNKNOWN}

    assert poll_payouts() == 0

    payout.refresh_from_db()
    assert payout.status == PayoutStatus.UNKNOWN


def test_poll_continues_after_provider_error(provider: FakePayoutProvider) -> None:
    _in_flight("tr_broken")
    ok = _in_flight("tr_ok")
    provider.broken = {"tr_broken"}
    provider.statuses = {"tr_ok": PayoutStatus.PAID}

    assert poll_payouts() == 1

    ok.refresh_from_db()
    assert ok.status == PayoutStatus.PAID
    assert provider.polled == ["tr_broken", "tr_ok"]


def test_poll_skips_created_terminal_and_unsent(provider: FakePayoutProvider) -> None:
    PayoutFactory.create(status=PayoutStatus.CREATED, provider_ref="tr_created")
    PayoutFactory.create(status=PayoutStatus.PAID, provider_ref="tr_done")
    PayoutFactory.create(status=PayoutStatus.PROCESSING, provider_ref="")

    assert poll_payouts() == 0
    assert provider.polled == []


def test_poll_is_idempotent(provider: FakePayoutProvider) -> None:
    _in_flight("tr_1")
    provider.statuses = {"tr_1": PayoutStatus.PAID}

    assert poll_payouts() == 1
    assert poll_payouts() == 0
    assert provider.polled == ["tr_1"]
