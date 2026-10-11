from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest

from payments.models import PaymentStatus, RefundStatus
from payments.providers.base import ChargeRequest, WebhookVerificationError
from payments.providers.stripe import StripeProvider

API_KEY = "test-api-key"
WEBHOOK_SECRET = "test-webhook-secret"
NOW = 1_700_000_000

Handler = Callable[[httpx.Request], httpx.Response]


def make_provider(handler: Handler | None = None) -> StripeProvider:
    transport = httpx.MockTransport(handler or (lambda _: httpx.Response(500)))
    return StripeProvider(
        httpx.Client(transport=transport, base_url="https://api.stripe.test"),
        secret_key=API_KEY,
        webhook_secret=WEBHOOK_SECRET,
        tolerance_seconds=300,
        clock=lambda: NOW,
    )


def sign(body: bytes, *, timestamp: int = NOW, secret: str = WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256)
    return "t=" + str(timestamp) + ",v1=" + digest.hexdigest()


def event_body(status: str = "succeeded") -> bytes:
    return json.dumps(
        {
            "id": "evt_1",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_1", "status": status}},
        }
    ).encode()


class TestCharges:
    def test_create_charge_sends_form_auth_and_idempotency_key(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "pi_1",
                    "status": "requires_payment_method",
                    "client_secret": "pi_1_secret",
                },
            )

        result = make_provider(handler).create_charge(
            ChargeRequest(
                amount_minor=2500,
                currency="EUR",
                idempotency_key="idem-1",
                description="Desk A1",
                metadata={"booking_id": "42"},
            )
        )

        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == "/v1/payment_intents"
        assert request.headers["Authorization"] == "Bearer " + API_KEY
        assert request.headers["Idempotency-Key"] == "idem-1"
        form = parse_qs(request.content.decode())
        assert form["amount"] == ["2500"]
        assert form["currency"] == ["eur"]
        assert form["automatic_payment_methods[enabled]"] == ["true"]
        assert form["description"] == ["Desk A1"]
        assert form["metadata[booking_id]"] == ["42"]

        assert result.provider_ref == "pi_1"
        assert result.status is PaymentStatus.PENDING
        assert result.client_secret == "pi_1_secret"
        assert result.raw_status == "requires_payment_method"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("processing", PaymentStatus.PENDING),
            ("succeeded", PaymentStatus.SUCCEEDED),
            ("canceled", PaymentStatus.FAILED),
            ("something_new", PaymentStatus.UNKNOWN),
        ],
    )
    def test_get_charge_maps_status(self, raw: str, expected: PaymentStatus) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/v1/payment_intents/pi_1"
            assert "Idempotency-Key" not in request.headers
            return httpx.Response(200, json={"id": "pi_1", "status": raw})

        assert make_provider(handler).get_charge("pi_1").status is expected

    def test_refund_posts_intent_and_amount(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": "re_1", "status": "succeeded"})

        result = make_provider(handler).refund(
            "pi_1", amount_minor=1000, currency="EUR", idempotency_key="refund-1"
        )

        request = seen[0]
        assert request.url.path == "/v1/refunds"
        assert request.headers["Idempotency-Key"] == "refund-1"
        assert parse_qs(request.content.decode()) == {
            "payment_intent": ["pi_1"],
            "amount": ["1000"],
        }
        assert result.provider_ref == "re_1"
        assert result.status is RefundStatus.SUCCEEDED


class TestWebhooks:
    def test_valid_signature_parses_event(self) -> None:
        body = event_body()

        event = make_provider().parse_webhook(body, {"stripe-signature": sign(body)})

        assert event.event_id == "evt_1"
        assert event.event_type == "payment_intent.succeeded"
        assert event.provider_ref == "pi_1"
        assert event.status is PaymentStatus.SUCCEEDED

    def test_any_matching_v1_signature_is_accepted(self) -> None:
        # Stripe sends several v1 values while a webhook secret is being rolled.
        body = event_body()
        header = sign(body).replace("v1=", "v1=deadbeef,v1=")

        assert make_provider().parse_webhook(body, {"Stripe-Signature": header}).event_id

    def test_missing_header_is_rejected(self) -> None:
        with pytest.raises(WebhookVerificationError, match="missing"):
            make_provider().parse_webhook(event_body(), {})

    @pytest.mark.parametrize("header", ["", "t=abc,v1=00", "t=1700000000", "v1=00"])
    def test_incomplete_header_is_rejected(self, header: str) -> None:
        with pytest.raises(WebhookVerificationError, match="components"):
            make_provider().parse_webhook(event_body(), {"Stripe-Signature": header})

    def test_wrong_secret_is_rejected(self) -> None:
        body = event_body()

        with pytest.raises(WebhookVerificationError, match="mismatch"):
            make_provider().parse_webhook(
                body, {"Stripe-Signature": sign(body, secret="other-secret")}
            )

    def test_tampered_body_is_rejected(self) -> None:
        signature = sign(event_body("canceled"))

        with pytest.raises(WebhookVerificationError, match="mismatch"):
            make_provider().parse_webhook(event_body(), {"Stripe-Signature": signature})

    @pytest.mark.parametrize("offset", [-301, 301])
    def test_timestamp_outside_tolerance_is_rejected(self, offset: int) -> None:
        body = event_body()

        with pytest.raises(WebhookVerificationError, match="tolerance"):
            make_provider().parse_webhook(
                body, {"Stripe-Signature": sign(body, timestamp=NOW + offset)}
            )

    @pytest.mark.parametrize("body", [b"not json", b'{"id": "evt_1"}'])
    def test_malformed_payload_is_rejected(self, body: bytes) -> None:
        with pytest.raises(WebhookVerificationError, match="malformed"):
            make_provider().parse_webhook(body, {"Stripe-Signature": sign(body)})
