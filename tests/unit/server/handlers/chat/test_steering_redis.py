"""Redis-level contract for the steer / reclaim primitives.

``wait_or_steer``'s accept-after-exit reclaim rests entirely on two Redis
facts that the higher-level tests only ever mock: ``steer_thread`` returns the
*exact* payload string it queued, and ``unsteer_thread`` removes that same
string by an exact-match ``LREM`` (True iff it was still there). These tests
exercise both against a stateful fake Redis so a wrong key, a wrong LREM count,
or a broken truthiness mapping fails here instead of silently in production.
"""

from __future__ import annotations

import json

import pytest

from .redis_fakes import FakeCache

CACHE = "src.utils.cache.redis_cache.get_cache_client"
KEY = "workflow:steering:t-1"


@pytest.mark.asyncio
async def test_steer_thread_queues_and_returns_the_exact_payload(monkeypatch):
    from src.server.handlers.chat.steering import steer_thread

    cache = FakeCache()
    monkeypatch.setattr(CACHE, lambda: cache)

    result = await steer_thread("t-1", "hello", "u-1")

    assert result is not None
    # The returned payload is byte-identical to what landed in the queue — the
    # reclaim's exact-match LREM depends on this identity.
    assert cache.client.lists[KEY] == [result["payload"]]
    assert result["position"] == 1
    body = json.loads(result["payload"])
    assert body["content"] == "hello" and body["user_id"] == "u-1"
    # An EXPIRE was issued so an unconsumed steer can't leak forever.
    assert KEY in cache.client.ttls


@pytest.mark.asyncio
async def test_unsteer_reclaims_the_just_queued_payload(monkeypatch):
    from src.server.handlers.chat.steering import steer_thread, unsteer_thread

    cache = FakeCache()
    monkeypatch.setattr(CACHE, lambda: cache)

    result = await steer_thread("t-1", "hello", "u-1")
    reclaimed = await unsteer_thread("t-1", result["payload"])

    assert reclaimed is True
    assert cache.client.lists[KEY] == []


@pytest.mark.asyncio
async def test_unsteer_false_when_a_drain_consumed_it_first(monkeypatch):
    """Drain won the race (the payload is already gone): LREM removes 0, so the
    caller must report accepted, not route fresh."""
    from src.server.handlers.chat.steering import steer_thread, unsteer_thread

    cache = FakeCache()
    monkeypatch.setattr(CACHE, lambda: cache)

    result = await steer_thread("t-1", "hello", "u-1")
    await cache.client.delete(KEY)  # simulate the exit drain's atomic wipe

    assert await unsteer_thread("t-1", result["payload"]) is False


@pytest.mark.asyncio
async def test_unsteer_only_removes_the_exact_payload(monkeypatch):
    """LREM is exact-match, not a blanket clear — a different steer left by
    another request must survive the reclaim."""
    from src.server.handlers.chat.steering import steer_thread, unsteer_thread

    cache = FakeCache()
    monkeypatch.setattr(CACHE, lambda: cache)

    mine = await steer_thread("t-1", "mine", "u-1")
    other = await steer_thread("t-1", "other", "u-2")

    assert await unsteer_thread("t-1", mine["payload"]) is True
    assert cache.client.lists[KEY] == [other["payload"]]


@pytest.mark.asyncio
async def test_unsteer_false_when_cache_disabled(monkeypatch):
    from src.server.handlers.chat.steering import unsteer_thread

    cache = FakeCache()
    cache.enabled = False
    monkeypatch.setattr(CACHE, lambda: cache)

    assert await unsteer_thread("t-1", "whatever") is False


@pytest.mark.asyncio
async def test_unsteer_false_on_redis_error(monkeypatch):
    """A Redis fault on the LREM must degrade to False (report accepted), never
    raise into the streaming generator."""
    from src.server.handlers.chat.steering import unsteer_thread

    cache = FakeCache()

    async def _boom(*_args):
        raise ConnectionError("redis down")

    cache.client.lrem = _boom
    monkeypatch.setattr(CACHE, lambda: cache)

    assert await unsteer_thread("t-1", "whatever") is False
