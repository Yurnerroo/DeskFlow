"""Thin httpx wrapper that turns transport failures into ProviderError.

Retryability is decided once, here: timeouts, connection errors, 429 and 5xx are
safe to retry *because* every mutating call carries an idempotency key.
"""

from __future__ import annotations

from typing import Any

import httpx

from payments.providers.base import ProviderError

RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES or status_code >= 500


def send(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderError(f"{method} {url} timed out", retryable=True) from exc
    except httpx.TransportError as exc:
        raise ProviderError(f"{method} {url} transport error: {exc}", retryable=True) from exc

    if response.is_error:
        raise ProviderError(
            f"{method} {url} failed with {response.status_code}: {response.text[:200]}",
            retryable=is_retryable_status(response.status_code),
        )
    data: dict[str, Any] = response.json()
    return data
