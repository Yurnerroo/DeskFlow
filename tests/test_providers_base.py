from collections.abc import Iterator, Mapping
from typing import ClassVar

import pytest

from payments.models import PaymentStatus, Provider, RefundStatus
from payments.providers import (
    BasePaymentProvider,
    ChargeRequest,
    ChargeResult,
    RefundResult,
    WebhookEvent,
    get_provider,
    register,
)
from payments.providers import registry as registry_module


class FakeProvider(BasePaymentProvider):
    provider = Provider.STRIPE
    status_map: ClassVar[Mapping[str, PaymentStatus]] = {
        "ok": PaymentStatus.SUCCEEDED,
        "bad": PaymentStatus.FAILED,
    }

    def create_charge(self, request: ChargeRequest) -> ChargeResult:
        return ChargeResult(provider_ref="fake_1", status=PaymentStatus.PENDING)

    def get_charge(self, provider_ref: str) -> ChargeResult:
        return ChargeResult(provider_ref=provider_ref, status=self.map_status("ok"))

    def refund(
        self, provider_ref: str, *, amount_minor: int, currency: str, idempotency_key: str
    ) -> RefundResult:
        return RefundResult(provider_ref="re_1", status=RefundStatus.SUCCEEDED)

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(registry_module, "_registry", {})
    yield


def test_unknown_raw_status_maps_to_unknown() -> None:
    provider = FakeProvider()

    assert provider.map_status("ok") == PaymentStatus.SUCCEEDED
    assert provider.map_status("brand_new_status") == PaymentStatus.UNKNOWN


@pytest.mark.usefixtures("isolated_registry")
def test_registry_resolves_by_enum_or_string() -> None:
    register(Provider.STRIPE)(FakeProvider)

    assert isinstance(get_provider(Provider.STRIPE), FakeProvider)
    assert isinstance(get_provider("stripe"), FakeProvider)


@pytest.mark.usefixtures("isolated_registry")
def test_registry_rejects_duplicates_and_unknowns() -> None:
    register(Provider.STRIPE)(FakeProvider)

    with pytest.raises(ValueError, match="already registered"):
        register(Provider.STRIPE)(FakeProvider)
    with pytest.raises(LookupError):
        get_provider(Provider.WISE)
    with pytest.raises(LookupError):
        get_provider("bitcoin")
