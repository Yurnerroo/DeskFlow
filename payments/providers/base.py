"""Provider-agnostic contract every payment gateway adapter implements.

The domain layer only ever sees these dataclasses and our own status enums, so
swapping or adding a gateway never leaks provider vocabulary into bookings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from payments.models import PaymentStatus, Provider, RefundStatus


class ProviderError(Exception):
    """A gateway call failed; `retryable` tells the caller whether to try again."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class WebhookVerificationError(ProviderError):
    """Signature, timestamp or payload of an inbound webhook is not trustworthy."""


@dataclass(frozen=True)
class ChargeRequest:
    amount_minor: int
    currency: str
    idempotency_key: str
    description: str = ""
    return_url: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChargeResult:
    provider_ref: str
    status: PaymentStatus
    client_secret: str = ""
    redirect_url: str = ""
    raw_status: str = ""


@dataclass(frozen=True)
class RefundResult:
    provider_ref: str
    status: RefundStatus


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    event_type: str
    provider_ref: str
    status: PaymentStatus
    payload: Mapping[str, Any]


class BasePaymentProvider(ABC):
    provider: ClassVar[Provider]
    status_map: ClassVar[Mapping[str, PaymentStatus]] = {}

    def map_status(self, raw_status: str) -> PaymentStatus:
        # Anything we do not recognise is UNKNOWN, never a guess: an UNKNOWN payment
        # is reconciled against the provider instead of confirming or expiring a booking.
        return self.status_map.get(raw_status, PaymentStatus.UNKNOWN)

    @abstractmethod
    def create_charge(self, request: ChargeRequest) -> ChargeResult: ...

    @abstractmethod
    def get_charge(self, provider_ref: str) -> ChargeResult: ...

    @abstractmethod
    def refund(
        self, provider_ref: str, *, amount_minor: int, currency: str, idempotency_key: str
    ) -> RefundResult: ...

    @abstractmethod
    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent: ...
