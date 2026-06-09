"""Unit tests for NewsCacheService.get_article_by_id.

Regression guard: the by-id scan previously called ``cache.keys()`` — a method
RedisCacheClient does not define — so it always raised AttributeError, was
swallowed, and returned None (the fast path never fired). It now uses the
SCAN-based ``scan_keys`` helper.
"""

from __future__ import annotations

import json

import pytest

from src.server.services.cache.news_cache_service import NewsCacheService

_GET_CACHE = "src.server.services.cache.news_cache_service.get_cache_client"


class _StubCache:
    """Minimal stand-in: scan_keys + get over an in-memory keyspace.

    Values are JSON strings, matching what RedisCacheClient.get returns for
    NewsCacheService entries (which are stored json.dumps-encoded).
    """

    def __init__(self, store: dict[str, str]):
        self._store = store

    async def scan_keys(self, pattern: str) -> list[str]:
        return list(self._store)

    async def get(self, key: str):
        return self._store.get(key)


@pytest.mark.asyncio
async def test_get_article_by_id_finds_match(monkeypatch):
    article = {"id": "abc", "title": "Top story", "article_url": "https://x/a"}
    store = {
        "news:general:20": json.dumps({"results": [{"id": "zzz"}]}),
        "news:tickertick:general:50": json.dumps({"results": [article]}),
    }
    monkeypatch.setattr(_GET_CACHE, lambda: _StubCache(store))

    found = await NewsCacheService().get_article_by_id("abc")
    assert found == article


@pytest.mark.asyncio
async def test_get_article_by_id_miss_returns_none(monkeypatch):
    store = {"news:general:20": json.dumps({"results": [{"id": "zzz"}]})}
    monkeypatch.setattr(_GET_CACHE, lambda: _StubCache(store))

    assert await NewsCacheService().get_article_by_id("abc") is None


@pytest.mark.asyncio
async def test_get_article_by_id_empty_keyspace(monkeypatch):
    monkeypatch.setattr(_GET_CACHE, lambda: _StubCache({}))
    assert await NewsCacheService().get_article_by_id("abc") is None
