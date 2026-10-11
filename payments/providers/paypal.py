"""PayPal Orders v2 adapter.

Differences from Stripe worth knowing:

* Auth is OAuth2 client-credentials; the bearer token is cached until shortly
  before it expires instead of being fetched per call.
* Idempotency uses the `PayPal-Request-Id` header.
* Amounts are decimal strings ("25.00"), not integer minor units.
* The payer approves the order on PayPal's site (redirect flow), then the order
  must be *captured*. Refunds target the capture id, not the order id.
* Webhooks are verified by asking PayPal (`verify-webhook-signature`) rather than
  by a local HMAC, so verification itself is a network call that can fail.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import httpx
from django.conf import settings

from payments.models import PaymentStatus, Provider, RefundStatus
from payments.providers import http
from payments.providers.base import (
    BasePaymentProvider,
    ChargeRequest,
    ChargeResult,
    ProviderError,
    RefundResult,
    WebhookEvent,
    WebhookVerificationError,
)
from payments.providers.registry import register

TOKEN_EXPIRY_MARGIN_SECONDS = 60

# PayPal rejects decimals for these currencies; everything else uses two places.
_ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})

_REFUND_STATUS: Mapping[str, RefundStatus] = {
    "PENDING": RefundStatus.PENDING,
    "COMPLETED": RefundStatus.SUCCEEDED,
    "FAILED": RefundStatus.FAILED,
    "CANCELLED": RefundStatus.FAILED,
}

_SIGNATURE_HEADERS: Mapping[str, str] = {
    "auth_algo": "PAYPAL-AUTH-ALGO",
    "cert_url": "PAYPAL-CERT-URL",
    "transmission_id": "PAYPAL-TRANSMISSION-ID",
    "transmission_sig": "PAYPAL-TRANSMISSION-SIG",
    "transmission_time": "PAYPAL-TRANSMISSION-TIME",
}


def format_amount(amount_minor: int, currency: str) -> str:
    if currency.upper() in _ZERO_DECIMAL_CURRENCIES:
        return str(amount_minor)
    sign = "-" if amount_minor < 0 else ""
    whole, cents = divmod(abs(amount_minor), 100)
    return f"{sign}{whole}.{cents:02d}"


@register(Provider.PAYPAL)
class PayPalProvider(BasePaymentProvider):
    provider: ClassVar[Provider] = Provider.PAYPAL
    status_map: ClassVar[Mapping[str, PaymentStatus]] = {
        "CREATED": PaymentStatus.PENDING,
        "SAVED": PaymentStatus.PENDING,
        "APPROVED": PaymentStatus.PENDING,
        "PAYER_ACTION_REQUIRED": PaymentStatus.PENDING,
        "PENDING": PaymentStatus.PENDING,
        "COMPLETED": PaymentStatus.SUCCEEDED,
        "VOIDED": PaymentStatus.FAILED,
        "DECLINED": PaymentStatus.FAILED,
        "DENIED": PaymentStatus.FAILED,
        "REFUNDED": PaymentStatus.REFUNDED,
    }

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        webhook_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id or settings.PAYPAL_CLIENT_ID
        self._client_secret = client_secret or settings.PAYPAL_CLIENT_SECRET
        self._webhook_id = webhook_id or settings.PAYPAL_WEBHOOK_ID
        self._clock = clock
        self._client = client or httpx.Client(
            base_url=settings.PAYPAL_BASE_URL,
            timeout=settings.PAYMENT_HTTP_TIMEOUT_SECONDS,
        )
        self._token = ""
        self._token_expires_at = 0.0

    # -- auth -----------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        credentials = f"{self._client_id}:{self._client_secret}".encode()
        data = http.send(
            self._client,
            "POST",
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": "Basic " + base64.b64encode(credentials).decode()},
        )
        try:
            token = str(data["access_token"])
            expires_in = int(data["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"malformed PayPal token response: {exc}") from exc
        self._token = token
        # Refresh early so a token never expires between "cache hit" and "request lands".
        self._token_expires_at = self._clock() + expires_in - TOKEN_EXPIRY_MARGIN_SECONDS
        return token

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers = {"Authorization": "Bearer " + self._access_token()}
        if request_id:
            # PayPal's idempotency key: a repeated id returns the original response.
            headers["PayPal-Request-Id"] = request_id
        return headers

    # -- charges --------------------------------------------------------------

    def _to_result(self, order: Mapping[str, Any]) -> ChargeResult:
        raw = str(order.get("status", ""))
        approve = next(
            (link["href"] for link in order.get("links", []) if link.get("rel") == "approve"),
            "",
        )
        return ChargeResult(
            provider_ref=str(order["id"]),
            status=self.map_status(raw),
            redirect_url=str(approve),
            raw_status=raw,
        )

    def create_charge(self, request: ChargeRequest) -> ChargeResult:
        unit: dict[str, Any] = {
            "amount": {
                "currency_code": request.currency.upper(),
                "value": format_amount(request.amount_minor, request.currency),
            },
        }
        if request.description:
            unit["description"] = request.description[:127]
        if booking_id := request.metadata.get("booking_id"):
            unit["custom_id"] = booking_id
        body: dict[str, Any] = {"intent": "CAPTURE", "purchase_units": [unit]}
        if request.return_url:
            body["application_context"] = {"return_url": request.return_url}

        order = http.send(
            self._client,
            "POST",
            "/v2/checkout/orders",
            json=body,
            headers=self._headers(request.idempotency_key),
        )
        return self._to_result(order)

    def get_charge(self, provider_ref: str) -> ChargeResult:
        order = http.send(
            self._client, "GET", f"/v2/checkout/orders/{provider_ref}", headers=self._headers()
        )
        return self._to_result(order)

    def capture(self, provider_ref: str, *, idempotency_key: str) -> ChargeResult:
        """Move an APPROVED order to COMPLETED. Called once the payer returns."""
        order = http.send(
            self._client,
            "POST",
            f"/v2/checkout/orders/{provider_ref}/capture",
            json={},
            headers=self._headers(idempotency_key),
        )
        return self._to_result(order)

    def refund(
        self, provider_ref: str, *, amount_minor: int, currency: str, idempotency_key: str
    ) -> RefundResult:
        capture_id = self._capture_id(provider_ref)
        refund = http.send(
            self._client,
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json={
                "amount": {
                    "currency_code": currency.upper(),
                    "value": format_amount(amount_minor, currency),
                }
            },
            headers=self._headers(idempotency_key),
        )
        return RefundResult(
            provider_ref=str(refund["id"]),
            status=_REFUND_STATUS.get(str(refund.get("status")), RefundStatus.PENDING),
        )

    def _capture_id(self, order_id: str) -> str:
        order = http.send(
            self._client, "GET", f"/v2/checkout/orders/{order_id}", headers=self._headers()
        )
        try:
            return str(order["purchase_units"][0]["payments"]["captures"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"PayPal order {order_id} has no capture to refund") from exc

    # -- webhooks -------------------------------------------------------------

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        signature = {field: _header(headers, name) for field, name in _SIGNATURE_HEADERS.items()}
        try:
            event = json.loads(body)
            if not isinstance(event, dict):
                raise TypeError("event is not an object")
        except (ValueError, TypeError) as exc:
            raise WebhookVerificationError(f"malformed PayPal event: {exc}") from exc

        self._verify_signature(signature, event)

        try:
            resource = event["resource"]
            # Capture events carry the capture id; the order id we stored lives here.
            related = resource.get("supplementary_data", {}).get("related_ids", {})
            return WebhookEvent(
                event_id=str(event["id"]),
                event_type=str(event["event_type"]),
                provider_ref=str(related.get("order_id") or resource["id"]),
                status=self.map_status(str(resource.get("status", ""))),
                payload=event,
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise WebhookVerificationError(f"malformed PayPal event: {exc}") from exc

    def _verify_signature(self, signature: Mapping[str, str], event: dict[str, Any]) -> None:
        response = http.send(
            self._client,
            "POST",
            "/v1/notifications/verify-webhook-signature",
            json={**signature, "webhook_id": self._webhook_id, "webhook_event": event},
            headers=self._headers(),
        )
        if response.get("verification_status") != "SUCCESS":
            raise WebhookVerificationError("PayPal webhook signature verification failed")


def _header(headers: Mapping[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    raise WebhookVerificationError(f"missing {name} header")
