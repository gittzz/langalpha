"""API-key leak guard for the FMP client's error path.

Every FMP request carries the API key as an ``apikey`` query param, so the
request URL is a secret. httpx bakes that URL into its exception messages
(``str(HTTPStatusError)`` → ``... for url 'https://...apikey=...'``).
``FMPClient._make_request`` must therefore never stringify the underlying httpx
error into ``FMPRequestError`` — it surfaces only the status code (or a static
message), so the key can't reach a caller, an agent, or a log line.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.data_client.fmp.fmp_client import FMPClient, FMPRequestError

_SECRET = "TESTSECRET"  # neutral placeholder — stands in for a real apikey


def _client_with_mocked_http(get_mock: AsyncMock) -> FMPClient:
    client = FMPClient(api_key=_SECRET)
    mock_http = AsyncMock()
    mock_http.is_closed = False  # else _get_client rebuilds a real client
    mock_http.get = get_mock
    client._client = mock_http
    return client


class TestFmpErrorKeyLeakGuard:
    @pytest.mark.asyncio
    async def test_http_status_error_does_not_leak_apikey(self):
        leaky_url = (
            f"https://financialmodelingprep.com/stable/profile?symbol=AAPL&apikey={_SECRET}"
        )
        request = httpx.Request("GET", leaky_url)
        response = httpx.Response(403, request=request, text="Forbidden")

        # Sanity: the RAW httpx error genuinely embeds the key via the URL, so
        # the guard below is load-bearing rather than testing a no-op.
        with pytest.raises(httpx.HTTPStatusError) as raw:
            response.raise_for_status()
        assert _SECRET in str(raw.value)

        client = _client_with_mocked_http(AsyncMock(return_value=response))
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client.get_profile("AAPL")
        finally:
            await client.close()

        assert _SECRET not in str(exc.value)
        assert exc.value.status_code == 403
        assert str(exc.value) == "FMP API request failed (403)"

    @pytest.mark.asyncio
    async def test_request_error_does_not_leak_apikey(self):
        # A transport-level error (ConnectError) whose message embeds the key.
        leaky = f"cannot connect to https://financialmodelingprep.com/stable/quote?apikey={_SECRET}"
        client = _client_with_mocked_http(AsyncMock(side_effect=httpx.ConnectError(leaky)))
        try:
            with pytest.raises(FMPRequestError) as exc:
                await client.get_quote("AAPL")
        finally:
            await client.close()

        assert _SECRET not in str(exc.value)
        assert str(exc.value) == "FMP API request failed"
        assert exc.value.status_code is None
