# Adapters register themselves with the registry as a side effect of being imported.
from payments.providers import paypal, stripe, wise  # noqa: F401
from payments.providers.base import (
    BasePaymentProvider,
    BasePayoutProvider,
    ChargeRequest,
    ChargeResult,
    PayoutRequest,
    PayoutResult,
    ProviderError,
    RefundResult,
    WebhookEvent,
    WebhookVerificationError,
)
from payments.providers.registry import (
    get_payout_provider,
    get_provider,
    register,
    register_payout,
)

__all__ = [
    "BasePaymentProvider",
    "BasePayoutProvider",
    "ChargeRequest",
    "ChargeResult",
    "PayoutRequest",
    "PayoutResult",
    "ProviderError",
    "RefundResult",
    "WebhookEvent",
    "WebhookVerificationError",
    "get_payout_provider",
    "get_provider",
    "register",
    "register_payout",
]
