import json
import uuid
from collections.abc import Callable

import httpx
import pytest

from payments.models import PayoutStatus, Provider
from payments.providers import registry
from payments.providers.base import PayoutRequest, ProviderError
from payments.providers.registry import get_payout_provider, register_payout
from payments.providers.wise import WiseProvider

Handler = Callable[[httpx.Request], httpx.Response]

PROFILE = "42"
QUOTE_PATH = f"/v3/profiles/{PROFILE}/quotes"
TRANSFER_PATH = "/v1/transfers"
FUND_PATH = f"/v3/profiles/{PROFILE}/transfers/T-1/payments"


def make_provider(handler: Handler) -> WiseProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.wise.test")
    return WiseProvider(client, api_token="tok", profile_id=PROFILE)


def payout_request(**overrides: object) -> PayoutRequest:
    fields: dict[str, object] = {
        "amount_minor": 1234,
        "currency": "eur",
        "idempotency_key": str(uuid.uuid4()),
        "recipient_id": "999",
        "reference": "booking 1",
    }
    fields.update(overrides)
    return PayoutRequest(**fields)  # type: ignore[arg-type]


def transfer(status: str = "incoming_payment_waiting", **extra: object) -> dict[str, object]:
    return {"id": "T-1", "status": status, **extra}


class Recorder:
    """Happy-path Wise backend that records every request it receives."""

    def __init__(self, transfer_status: str = "incoming_payment_waiting") -> None:
        self.requests: list[httpx.Request] = []
        self.transfer_status = transfer_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == QUOTE_PATH:
            return httpx.Response(200, json={"id": "Q-1"})
        if path == TRANSFER_PATH:
            return httpx.Response(200, json=transfer(self.transfer_status))
        if path == FUND_PATH:
            return httpx.Response(201, json={"type": "BALANCE", "status": "COMPLETED"})
        return httpx.Response(404)

    def body(self, index: int) -> dict[str, object]:
        data: dict[str, object] = json.loads(self.requests[index].content)
        return data

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


# -- create_payout ------------------------------------------------------------


def test_create_payout_quotes_transfers_then_funds() -> None:
    backend = Recorder()
    result = make_provider(backend).create_payout(payout_request())

    assert backend.paths == [QUOTE_PATH, TRANSFER_PATH, FUND_PATH]
    assert all(r.method == "POST" for r in backend.requests)
    assert backend.body(2) == {"type": "BALANCE"}
    assert result.provider_ref == "T-1"
    assert result.status == PayoutStatus.PROCESSING
    assert result.raw_status == "incoming_payment_waiting"
    assert result.failure_reason == ""


@pytest.mark.parametrize("status", ["processing", "outgoing_payment_sent"])
def test_create_payout_does_not_fund_an_already_funded_transfer(status: str) -> None:
    backend = Recorder(transfer_status=status)
    result = make_provider(backend).create_payout(payout_request())

    assert backend.paths == [QUOTE_PATH, TRANSFER_PATH]
    assert result.raw_status == status


def test_quote_body_uses_decimal_target_amount() -> None:
    backend = Recorder()
    make_provider(backend).create_payout(payout_request(amount_minor=1234, currency="eur"))

    assert backend.body(0) == {
        "sourceCurrency": "EUR",
        "targetCurrency": "EUR",
        "targetAmount": 12.34,
        "payOut": "BALANCE",
    }


def test_quote_body_handles_zero_decimal_currency() -> None:
    backend = Recorder()
    make_provider(backend).create_payout(payout_request(amount_minor=1500, currency="JPY"))

    assert backend.body(0)["targetAmount"] == 1500.0


def test_transfer_body_carries_idempotency_key_and_truncated_reference() -> None:
    backend = Recorder()
    key = str(uuid.uuid4())
    make_provider(backend).create_payout(
        payout_request(idempotency_key=key, reference="x" * 50, recipient_id="777")
    )

    body = backend.body(1)
    assert body["customerTransactionId"] == key
    assert body["quoteUuid"] == "Q-1"
    assert body["targetAccount"] == "777"
    assert body["details"] == {"reference": "x" * 35}


def test_every_call_sends_bearer_token() -> None:
    backend = Recorder()
    make_provider(backend).create_payout(payout_request())

    assert {r.headers["Authorization"] for r in backend.requests} == {"Bearer tok"}


def test_malformed_quote_raises_provider_error() -> None:
    provider = make_provider(lambda _: httpx.Response(200, json={"rate": 1}))

    with pytest.raises(ProviderError, match="malformed Wise quote"):
        provider.create_payout(payout_request())


def test_malformed_transfer_raises_provider_error_and_skips_funding() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == QUOTE_PATH:
            return httpx.Response(200, json={"id": "Q-1"})
        return httpx.Response(200, json={"id": "T-1"})

    with pytest.raises(ProviderError, match="malformed Wise transfer"):
        make_provider(handler).create_payout(payout_request())
    assert calls == [QUOTE_PATH, TRANSFER_PATH]


def test_server_error_is_retryable() -> None:
    provider = make_provider(lambda _: httpx.Response(503, json={}))

    with pytest.raises(ProviderError) as exc_info:
        provider.create_payout(payout_request())
    assert exc_info.value.retryable is True


def test_client_error_is_not_retryable() -> None:
    provider = make_provider(lambda _: httpx.Response(400, json={"error": "bad"}))

    with pytest.raises(ProviderError) as exc_info:
        provider.create_payout(payout_request())
    assert exc_info.value.retryable is False


# -- get_payout / status mapping ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("incoming_payment_waiting", PayoutStatus.PROCESSING),
        ("incoming_payment_initiated", PayoutStatus.PROCESSING),
        ("processing", PayoutStatus.PROCESSING),
        ("funds_converted", PayoutStatus.PROCESSING),
        ("outgoing_payment_sent", PayoutStatus.PAID),
        ("funds_refunded", PayoutStatus.FAILED),
        ("cancelled", PayoutStatus.FAILED),
        ("bounced_back", PayoutStatus.FAILED),
        ("charged_back", PayoutStatus.FAILED),
        ("something_new", PayoutStatus.UNKNOWN),
    ],
)
def test_get_payout_maps_status(raw: str, expected: PayoutStatus) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=transfer(raw))

    result = make_provider(handler).get_payout("T-1")

    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/transfers/T-1"
    assert result.status == expected
    assert result.raw_status == raw


def test_failed_status_carries_failure_reason() -> None:
    provider = make_provider(lambda _: httpx.Response(200, json=transfer("bounced_back")))

    result = provider.get_payout("T-1")

    assert result.failure_reason == "Wise status bounced_back"


def test_non_failed_status_has_no_failure_reason() -> None:
    provider = make_provider(lambda _: httpx.Response(200, json=transfer("outgoing_payment_sent")))

    assert provider.get_payout("T-1").failure_reason == ""


# -- registry -------------------------------------------------------------------


def test_registry_returns_wise_for_payouts() -> None:
    assert isinstance(get_payout_provider(Provider.WISE), WiseProvider)
    assert isinstance(get_payout_provider("wise"), WiseProvider)


def test_charge_providers_are_not_payout_providers() -> None:
    with pytest.raises(LookupError):
        get_payout_provider(Provider.STRIPE)


def test_unknown_payout_provider_raises_lookup_error() -> None:
    with pytest.raises(LookupError):
        get_payout_provider("nope")


def test_duplicate_payout_registration_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_payout_registry", dict(registry._payout_registry))

    with pytest.raises(ValueError, match="already registered"):
        register_payout(Provider.WISE)(WiseProvider)
