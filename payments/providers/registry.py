from __future__ import annotations

from collections.abc import Callable

from payments.models import Provider
from payments.providers.base import BasePaymentProvider

ProviderFactory = Callable[[], BasePaymentProvider]

_registry: dict[Provider, ProviderFactory] = {}


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
