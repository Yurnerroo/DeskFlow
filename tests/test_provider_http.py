from __future__ import annotations

import httpx
import pytest

from payments.providers import http
from payments.providers.base import ProviderError


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler, base_url="https://provider.test")


def test_success_returns_json_body() -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "x"})))

    assert http.send(client, "GET", "/thing") == {"id": "x"}


@pytest.mark.parametrize("status_code", [408, 409, 425, 429, 500, 502, 503])
def test_transient_status_codes_are_retryable(status_code: int) -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(status_code, text="nope")))

    with pytest.raises(ProviderError) as exc_info:
        http.send(client, "POST", "/thing")

    assert exc_info.value.retryable is True


@pytest.mark.parametrize("status_code", [400, 401, 402, 404, 422])
def test_client_errors_are_not_retryable(status_code: int) -> None:
    client = _client(httpx.MockTransport(lambda _: httpx.Response(status_code, text="bad")))

    with pytest.raises(ProviderError) as exc_info:
        http.send(client, "POST", "/thing")

    assert exc_info.value.retryable is False
    assert str(status_code) in str(exc_info.value)


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")],
    ids=["timeout", "connect"],
)
def test_transport_failures_are_retryable(error: Exception) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise error

    client = _client(httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as exc_info:
        http.send(client, "POST", "/thing")

    assert exc_info.value.retryable is True
