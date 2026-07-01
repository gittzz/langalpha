"""Unit tests for the yfinance data source snapshots — no network."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.data_client.yfinance.data_source import YFinanceDataSource


@pytest.mark.asyncio
async def test_get_snapshots_returns_bare_index_symbol():
    """Indices are queried from Yahoo with a caret ("^GSPC"), but the snapshot
    must echo back the bare requested symbol ("GSPC") so the provider chain
    matches it instead of dropping it as unrequested. Regression for #287.
    """
    def fake_fetch(sym: str) -> dict:
        # Mirrors the real fetch: echoes whatever symbol it was queried with.
        return {"symbol": sym, "price": 5000.0}

    with patch(
        "src.data_client.yfinance.data_source._fetch_single_snapshot",
        side_effect=fake_fetch,
    ):
        result = await YFinanceDataSource().get_snapshots(
            ["GSPC"], asset_type="indices"
        )

    assert result == [{"symbol": "GSPC", "price": 5000.0}]


@pytest.mark.asyncio
async def test_get_snapshots_preserves_order_and_drops_failures():
    """Symbol restoration stays aligned when a fetch returns None."""
    def fake_fetch(sym: str) -> dict | None:
        return None if sym == "^IXIC" else {"symbol": sym, "price": 1.0}

    with patch(
        "src.data_client.yfinance.data_source._fetch_single_snapshot",
        side_effect=fake_fetch,
    ):
        result = await YFinanceDataSource().get_snapshots(
            ["GSPC", "IXIC", "DJI"], asset_type="indices"
        )

    assert [r["symbol"] for r in result] == ["GSPC", "DJI"]


@pytest.mark.asyncio
async def test_get_snapshots_stocks_pass_symbol_through_unchanged():
    """Stocks aren't caret-prefixed; the symbol is returned as requested."""
    def fake_fetch(sym: str) -> dict:
        return {"symbol": sym, "price": 190.0}

    with patch(
        "src.data_client.yfinance.data_source._fetch_single_snapshot",
        side_effect=fake_fetch,
    ):
        result = await YFinanceDataSource().get_snapshots(["AAPL"])

    assert result == [{"symbol": "AAPL", "price": 190.0}]
