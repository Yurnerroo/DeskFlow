import json
from collections.abc import Callable
from typing import ClassVar

import httpx
import pytest

from payments.models import PaymentStatus, RefundStatus
from payments.providers.base import ChargeRequest, ProviderError, WebhookVerificationError
from payments.providers.paypal import PayPalProvider, format_amount

Handler = Callable[[httpx.Request], httpx.Response]

NOW = 1_700_000_000.0

SIGNATURE_HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.paypal.test/cert.pem",
    "PAYPAL-TRANSMISSION-ID": "tx-1",
    "PAYPAL-TRANSMISSION-SIG": "sig",
    "PAYPAL-TRANSMISSION-TIME": "2024-01-01T00:00:00Z",
}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def token_response(expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok", "expires_in": expires_in})


def with_token(handler: Handler) -> Handler:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        return handler(request)

    return wrapped


def make_provider(handler: Handler | None = None, clock: Clock | None = None) -> PayPalProvider:
    transport = httpx.MockTransport(handler or (lambda _: httpx.Response(500)))
    return PayPalProvider(
        httpx.Client(transport=transport, base_url="https://api.paypal.test"),
        client_id="cid",
        client_secret="secret",
        webhook_id="wh-1",
        clock=clock or Clock(),
    )


def order(status: str = "CREATED", **extra: object) -> dict[str, object]:
    return {
        "id": "ORDER-1",
        "status": status,
        "links": [
            {"rel": "self", "href": "https://api.paypal.test/v2/checkout/orders/ORDER-1"},
            {"rel": "approve", "href": "https://paypal.test/checkoutnow?token=ORDER-1"},
        ],
        **extra,
    }


@pytest.mark.parametrize(
    ("minor", "currency", "expected"),
    [(2500, "usd", "25.00"), (5, "EUR", "0.05"), (1999, "GBP", "19.99"), (1500, "JPY", "1500")],
)
def test_format_amount(minor: int, currency: str, expected: str) -> None:
    assert format_amount(minor, currency) == expected


class TestAuth:
    def test_token_is_fetched_with_basic_auth_and_cached(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/oauth2/token":
                return token_response()
            return httpx.Response(200, json=order())

        provider = make_provider(handler)
        provider.get_charge("ORDER-1")
        provider.get_charge("ORDER-1")

        token_calls = [r for r in seen if r.url.path == "/v1/oauth2/token"]
        assert len(token_calls) == 1
        assert token_calls[0].headers["Authorization"].startswith("Basic ")
        assert token_calls[0].content == b"grant_type=client_credentials"
        assert seen[-1].headers["Authorization"] == "Bearer " + "tok"

    def test_token_is_refreshed_before_expiry(self) -> None:
        token_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.path == "/v1/oauth2/token":
                token_calls += 1
                return token_response(expires_in=300)
            return httpx.Response(200, json=order())

        clock = Clock()
        provider = make_provider(handler, clock)
        provider.get_charge("ORDER-1")
        clock.now += 239
        provider.get_charge("ORDER-1")
        assert token_calls == 1

        clock.now += 1  # now inside the 60s safety margin
        provider.get_charge("ORDER-1")
        assert token_calls == 2

    def test_malformed_token_response_raises(self) -> None:
        provider = make_provider(lambda _: httpx.Response(200, json={"nope": 1}))
        with pytest.raises(ProviderError, match="malformed PayPal token"):
            provider.get_charge("ORDER-1")


class TestCharges:
    def test_create_order_sends_decimal_amount_and_request_id(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201, json=order())

        provider = make_provider(with_token(handler))
        result = provider.create_charge(
            ChargeRequest(
                amount_minor=2500,
                currency="eur",
                idempotency_key="booking-42-attempt-1",
                description="Desk A1",
                return_url="https://deskflow.test/return",
                metadata={"booking_id": "42"},
            )
        )

        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == "/v2/checkout/orders"
        assert request.headers["PayPal-Request-Id"] == "booking-42-attempt-1"
        body = json.loads(request.content)
        assert body["intent"] == "CAPTURE"
        unit = body["purchase_units"][0]
        assert unit["amount"] == {"currency_code": "EUR", "value": "25.00"}
        assert unit["custom_id"] == "42"
        assert unit["description"] == "Desk A1"
        assert body["application_context"]["return_url"] == "https://deskflow.test/return"

        assert result.provider_ref == "ORDER-1"
        assert result.status == PaymentStatus.PENDING
        assert result.redirect_url == "https://paypal.test/checkoutnow?token=ORDER-1"
        assert result.raw_status == "CREATED"

    def test_optional_fields_are_omitted(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201, json=order())

        make_provider(with_token(handler)).create_charge(
            ChargeRequest(amount_minor=100, currency="usd", idempotency_key="k")
        )
        body = json.loads(seen[0].content)
        assert "application_context" not in body
        assert "custom_id" not in body["purchase_units"][0]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("CREATED", PaymentStatus.PENDING),
            ("APPROVED", PaymentStatus.PENDING),
            ("PAYER_ACTION_REQUIRED", PaymentStatus.PENDING),
            ("COMPLETED", PaymentStatus.SUCCEEDED),
            ("VOIDED", PaymentStatus.FAILED),
            ("SOMETHING_NEW", PaymentStatus.UNKNOWN),
        ],
    )
    def test_get_order_maps_status(self, raw: str, expected: PaymentStatus) -> None:
        provider = make_provider(with_token(lambda _: httpx.Response(200, json=order(raw))))
        assert provider.get_charge("ORDER-1").status == expected

    def test_capture_posts_to_capture_endpoint(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201, json=order("COMPLETED"))

        result = make_provider(with_token(handler)).capture("ORDER-1", idempotency_key="cap-1")
        assert seen[0].url.path == "/v2/checkout/orders/ORDER-1/capture"
        assert seen[0].headers["PayPal-Request-Id"] == "cap-1"
        assert result.status == PaymentStatus.SUCCEEDED

    def test_server_error_is_retryable(self) -> None:
        provider = make_provider(with_token(lambda _: httpx.Response(503)))
        with pytest.raises(ProviderError) as exc_info:
            provider.get_charge("ORDER-1")
        assert exc_info.value.retryable


class TestRefunds:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("COMPLETED", RefundStatus.SUCCEEDED),
            ("PENDING", RefundStatus.PENDING),
            ("CANCELLED", RefundStatus.FAILED),
            ("FAILED", RefundStatus.FAILED),
        ],
    )
    def test_refund_resolves_capture_and_maps_status(
        self, raw: str, expected: RefundStatus
    ) -> None:
        seen: list[httpx.Request] = []
        captured = order(
            "COMPLETED", purchase_units=[{"payments": {"captures": [{"id": "CAP-9"}]}}]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=captured)
            return httpx.Response(201, json={"id": "REF-1", "status": raw})

        result = make_provider(with_token(handler)).refund(
            "ORDER-1", amount_minor=1050, currency="usd", idempotency_key="refund-1"
        )

        post = seen[-1]
        assert post.url.path == "/v2/payments/captures/CAP-9/refund"
        assert post.headers["PayPal-Request-Id"] == "refund-1"
        assert json.loads(post.content) == {"amount": {"currency_code": "USD", "value": "10.50"}}
        assert result.provider_ref == "REF-1"
        assert result.status == expected

    def test_refund_without_capture_raises(self) -> None:
        provider = make_provider(with_token(lambda _: httpx.Response(200, json=order())))
        with pytest.raises(ProviderError, match="no capture"):
            provider.refund("ORDER-1", amount_minor=100, currency="usd", idempotency_key="r")


class TestWebhooks:
    EVENT: ClassVar[dict[str, object]] = {
        "id": "WH-EVT-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-9",
            "status": "COMPLETED",
            "supplementary_data": {"related_ids": {"order_id": "ORDER-1"}},
        },
    }

    def verifier(self, status: str, seen: list[httpx.Request] | None = None) -> Handler:
        def handler(request: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen.append(request)
            return httpx.Response(200, json={"verification_status": status})

        return with_token(handler)

    def test_verified_event_is_parsed(self) -> None:
        seen: list[httpx.Request] = []
        provider = make_provider(self.verifier("SUCCESS", seen))

        event = provider.parse_webhook(json.dumps(self.EVENT).encode(), SIGNATURE_HEADERS)

        assert event.event_id == "WH-EVT-1"
        assert event.event_type == "PAYMENT.CAPTURE.COMPLETED"
        assert event.provider_ref == "ORDER-1"
        assert event.status == PaymentStatus.SUCCEEDED

        sent = json.loads(seen[0].content)
        assert seen[0].url.path == "/v1/notifications/verify-webhook-signature"
        assert sent["webhook_id"] == "wh-1"
        assert sent["transmission_id"] == "tx-1"
        assert sent["auth_algo"] == "SHA256withRSA"
        assert sent["webhook_event"] == self.EVENT

    def test_headers_are_case_insensitive(self) -> None:
        headers = {k.lower(): v for k, v in SIGNATURE_HEADERS.items()}
        provider = make_provider(self.verifier("SUCCESS"))
        event = provider.parse_webhook(json.dumps(self.EVENT).encode(), headers)
        assert event.event_id == "WH-EVT-1"

    def test_order_event_falls_back_to_resource_id(self) -> None:
        payload = {
            "id": "WH-EVT-2",
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {"id": "ORDER-7", "status": "APPROVED"},
        }
        provider = make_provider(self.verifier("SUCCESS"))
        event = provider.parse_webhook(json.dumps(payload).encode(), SIGNATURE_HEADERS)
        assert event.provider_ref == "ORDER-7"
        assert event.status == PaymentStatus.PENDING

    def test_failed_verification_raises(self) -> None:
        provider = make_provider(self.verifier("FAILURE"))
        with pytest.raises(WebhookVerificationError, match="verification failed"):
            provider.parse_webhook(json.dumps(self.EVENT).encode(), SIGNATURE_HEADERS)

    @pytest.mark.parametrize("missing", list(SIGNATURE_HEADERS))
    def test_missing_header_raises_without_network(self, missing: str) -> None:
        headers = {k: v for k, v in SIGNATURE_HEADERS.items() if k != missing}
        provider = make_provider()  # any HTTP call would 500
        with pytest.raises(WebhookVerificationError, match=f"missing {missing}"):
            provider.parse_webhook(json.dumps(self.EVENT).encode(), headers)

    @pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
    def test_malformed_body_raises(self, body: bytes) -> None:
        provider = make_provider()
        with pytest.raises(WebhookVerificationError, match="malformed"):
            provider.parse_webhook(body, SIGNATURE_HEADERS)

    def test_verified_event_without_resource_raises(self) -> None:
        payload = {"id": "WH-EVT-3", "event_type": "X"}
        provider = make_provider(self.verifier("SUCCESS"))
        with pytest.raises(WebhookVerificationError, match="malformed"):
            provider.parse_webhook(json.dumps(payload).encode(), SIGNATURE_HEADERS)
