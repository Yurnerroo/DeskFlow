from __future__ import annotations

from collections.abc import Callable

from payments.models import Provider
from payments.providers.base import BasePaymentProvider, BasePayoutProvider

ProviderFactory = Callable[[], BasePaymentProvider]
PayoutProviderFactory = Callable[[], BasePayoutProvider]

_registry: dict[Provider, ProviderFactory] = {}
# Kept separate so a payout-only rail can never be looked up to take a charge.
_payout_registry: dict[Provider, PayoutProviderFactory] = {}


def register(provider: Provider) -> Callable[[ProviderFactory], ProviderFactory]:
    def decorator(factory: ProviderFactory) -> ProviderFactory:
        if provider in _registry:
            raise ValueError(f"Provider {provider} is already registered")
        _registry[provider] = factory
        return factory

    return decorator


def get_provider(provider: Provider | str) -> BasePaymentProvider:
    try:
        factory = _registry[Provider(str(provider).upper())]
    except (KeyError, ValueError) as exc:
        raise LookupError(f"No payment provider registered for {provider!r}") from exc
    return factory()


def register_payout(
    provider: Provider,
) -> Callable[[PayoutProviderFactory], PayoutProviderFactory]:
    def decorator(factory: PayoutProviderFactory) -> PayoutProviderFactory:
        if provider in _payout_registry:
            raise ValueError(f"Payout provider {provider} is already registered")
        _payout_registry[provider] = factory
        return factory

    return decorator


def get_payout_provider(provider: Provider | str) -> BasePayoutProvider:
    try:
        factory = _payout_registry[Provider(str(provider).upper())]
    except (KeyError, ValueError) as exc:
        raise LookupError(f"No payout provider registered for {provider!r}") from exc
    return factory()
