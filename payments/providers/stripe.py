"""Stripe PaymentIntents adapter.

Talks to the REST API directly with httpx (no SDK) so every wire-level detail
is visible: form encoding, the Idempotency-Key header and the signed webhook
scheme `Stripe-Signature: t=<unix>,v1=<hex hmac>`.
"""

from __future__ import annotations

import hashlib
import hmac
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
    RefundResult,
    WebhookEvent,
    WebhookVerificationError,
)
from payments.providers.registry import register

_REFUND_STATUS: Mapping[str, RefundStatus] = {
    "pending": RefundStatus.PENDING,
    "requires_action": RefundStatus.PENDING,
    "succeeded": RefundStatus.SUCCEEDED,
    "failed": RefundStatus.FAILED,
    "canceled": RefundStatus.FAILED,
}


@register(Provider.STRIPE)
class StripeProvider(BasePaymentProvider):
    provider: ClassVar[Provider] = Provider.STRIPE
    status_map: ClassVar[Mapping[str, PaymentStatus]] = {
        "requires_payment_method": PaymentStatus.PENDING,
        "requires_confirmation": PaymentStatus.PENDING,
        "requires_action": PaymentStatus.PENDING,
        "processing": PaymentStatus.PENDING,
        "succeeded": PaymentStatus.SUCCEEDED,
        "canceled": PaymentStatus.FAILED,
    }

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        secret_key: str | None = None,
        webhook_secret: str | None = None,
        tolerance_seconds: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret_key = secret_key or settings.STRIPE_SECRET_KEY
        self._webhook_secret = webhook_secret or settings.STRIPE_WEBHOOK_SECRET
        self._tolerance = (
            tolerance_seconds
            if tolerance_seconds is not None
            else settings.WEBHOOK_TOLERANCE_SECONDS
        )
        self._clock = clock
        self._client = client or httpx.Client(
            base_url=settings.STRIPE_API_BASE,
            timeout=settings.PAYMENT_HTTP_TIMEOUT_SECONDS,
        )

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": "Bearer " + self._secret_key}
        if idempotency_key:
            # Stripe replays the original response for a repeated key, so a retry
            # after a timeout can never create a second PaymentIntent.
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _to_result(self, intent: Mapping[str, Any]) -> ChargeResult:
        raw = str(intent.get("status", ""))
        return ChargeResult(
            provider_ref=str(intent["id"]),
            status=self.map_status(raw),
            client_secret=str(intent.get("client_secret") or ""),
            raw_status=raw,
        )

    def create_charge(self, request: ChargeRequest) -> ChargeResult:
        form: dict[str, str] = {
            "amount": str(request.amount_minor),
            "currency": request.currency.lower(),
            "automatic_payment_methods[enabled]": "true",
        }
        if request.description:
            form["description"] = request.description
        for key, value in request.metadata.items():
            form[f"metadata[{key}]"] = value
        intent = http.send(
            self._client,
            "POST",
            "/v1/payment_intents",
            data=form,
            headers=self._headers(request.idempotency_key),
        )
        return self._to_result(intent)

    def get_charge(self, provider_ref: str) -> ChargeResult:
        intent = http.send(
            self._client, "GET", f"/v1/payment_intents/{provider_ref}", headers=self._headers()
        )
        return self._to_result(intent)

    def refund(
        self, provider_ref: str, *, amount_minor: int, currency: str, idempotency_key: str
    ) -> RefundResult:
        refund = http.send(
            self._client,
            "POST",
            "/v1/refunds",
            data={"payment_intent": provider_ref, "amount": str(amount_minor)},
            headers=self._headers(idempotency_key),
        )
        return RefundResult(
            provider_ref=str(refund["id"]),
            status=_REFUND_STATUS.get(str(refund.get("status")), RefundStatus.PENDING),
        )

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        signature = _header(headers, "Stripe-Signature")
        self._verify_signature(body, signature)
        try:
            event = json.loads(body)
            obj = event["data"]["object"]
            return WebhookEvent(
                event_id=str(event["id"]),
                event_type=str(event["type"]),
                provider_ref=str(obj["id"]),
                status=self.map_status(str(obj.get("status", ""))),
                payload=event,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise WebhookVerificationError(f"malformed Stripe event: {exc}") from exc

    def _verify_signature(self, body: bytes, header: str) -> None:
        timestamp = ""
        candidates: list[str] = []
        for part in header.split(","):
            key, _, value = part.strip().partition("=")
            if key == "t":
                timestamp = value
            elif key == "v1":
                candidates.append(value)
        if not timestamp.isdigit() or not candidates:
            raise WebhookVerificationError("missing Stripe-Signature components")

        # Bounding the timestamp is what defeats replay of a captured, validly signed body.
        if abs(self._clock() - int(timestamp)) > self._tolerance:
            raise WebhookVerificationError("Stripe webhook timestamp outside tolerance")

        signed_payload = timestamp.encode() + b"." + body
        expected = hmac.new(
            self._webhook_secret.encode(), signed_payload, hashlib.sha256
        ).hexdigest()
        # Constant-time comparison: a byte-by-byte `==` leaks how much of the MAC matched.
        if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
            raise WebhookVerificationError("Stripe webhook signature mismatch")


def _header(headers: Mapping[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    raise WebhookVerificationError(f"missing {name} header")
