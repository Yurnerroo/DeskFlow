"""Wise (TransferWise) payout adapter.

Differences from the charge providers worth knowing:

* Wise is a payout rail: money goes *out* to a recipient, so it implements
  `BasePayoutProvider`, not `BasePaymentProvider`.
* A payout is three calls: create a quote (locks the rate), create a transfer
  against that quote, then fund the transfer from the profile balance.
* Idempotency is a body field (`customerTransactionId`, must be a UUID), not a
  header. Retrying transfer creation with the same id returns the original
  transfer instead of sending the money twice.
* The quote and funding calls are not idempotent on their own. A duplicate
  quote is harmless (it expires unused); funding an already-funded transfer is
  rejected, so funding only happens while the transfer is still waiting for it.
* Amounts are decimal numbers in the JSON body, not integer minor units.
* Status is polled (`GET /v1/transfers/{id}`); there is no synchronous "paid".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import httpx
from django.conf import settings

from payments.models import PayoutStatus, Provider
from payments.providers import http
from payments.providers.base import (
    BasePayoutProvider,
    PayoutRequest,
    PayoutResult,
    ProviderError,
)
from payments.providers.paypal import format_amount
from payments.providers.registry import register_payout

_AWAITING_FUNDING = "incoming_payment_waiting"


@register_payout(Provider.WISE)
class WiseProvider(BasePayoutProvider):
    provider: ClassVar[Provider] = Provider.WISE
    status_map: ClassVar[Mapping[str, PayoutStatus]] = {
        "incoming_payment_waiting": PayoutStatus.PROCESSING,
        "incoming_payment_initiated": PayoutStatus.PROCESSING,
        "processing": PayoutStatus.PROCESSING,
        "funds_converted": PayoutStatus.PROCESSING,
        "outgoing_payment_sent": PayoutStatus.PAID,
        "funds_refunded": PayoutStatus.FAILED,
        "cancelled": PayoutStatus.FAILED,
        "bounced_back": PayoutStatus.FAILED,
        "charged_back": PayoutStatus.FAILED,
    }

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        api_token: str | None = None,
        profile_id: str | None = None,
    ) -> None:
        self._api_token = api_token or settings.WISE_API_TOKEN
        self._profile_id = profile_id or settings.WISE_PROFILE_ID
        self._client = client or httpx.Client(
            base_url=settings.WISE_BASE_URL,
            timeout=settings.PAYMENT_HTTP_TIMEOUT_SECONDS,
        )

    # -- auth -----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._api_token}

    # -- payouts --------------------------------------------------------------

    def _to_result(self, transfer: Mapping[str, Any]) -> PayoutResult:
        try:
            raw = str(transfer["status"])
            ref = str(transfer["id"])
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"malformed Wise transfer response: {exc}") from exc
        status = self.map_status(raw)
        reason = f"Wise status {raw}" if status == PayoutStatus.FAILED else ""
        return PayoutResult(provider_ref=ref, status=status, raw_status=raw, failure_reason=reason)

    def create_payout(self, request: PayoutRequest) -> PayoutResult:
        currency = request.currency.upper()
        quote = http.send(
            self._client,
            "POST",
            f"/v3/profiles/{self._profile_id}/quotes",
            json={
                "sourceCurrency": currency,
                "targetCurrency": currency,
                "targetAmount": float(format_amount(request.amount_minor, currency)),
                "payOut": "BALANCE",
            },
            headers=self._headers(),
        )
        try:
            quote_id = str(quote["id"])
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"malformed Wise quote response: {exc}") from exc

        transfer = http.send(
            self._client,
            "POST",
            "/v1/transfers",
            json={
                "targetAccount": request.recipient_id,
                "quoteUuid": quote_id,
                "customerTransactionId": request.idempotency_key,
                "details": {"reference": request.reference[:35]},
            },
            headers=self._headers(),
        )
        result = self._to_result(transfer)
        # A retry returns the original transfer; if it was already funded, stop here.
        if result.raw_status != _AWAITING_FUNDING:
            return result

        http.send(
            self._client,
            "POST",
            f"/v3/profiles/{self._profile_id}/transfers/{result.provider_ref}/payments",
            json={"type": "BALANCE"},
            headers=self._headers(),
        )
        return result

    def get_payout(self, provider_ref: str) -> PayoutResult:
        transfer = http.send(
            self._client, "GET", f"/v1/transfers/{provider_ref}", headers=self._headers()
        )
        return self._to_result(transfer)
