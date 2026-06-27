"""Coverage for the report-back dispatch caps (per-flash + per-user).

A flash thread can fan out many background PTC analyses, but unbounded fan-out
would overload the single backend. ``_reserve_dispatch_slot`` atomically admits
a dispatch under both caps *before* the dispatch POST (rolled back on failure),
so racing calls can't both pass the check then overshoot.
"""

from __future__ import annotations

import json

import pytest

from src.tools.secretary import tools as T


class _FakeClient:
    def __init__(self) -> None:
        self.sets: dict[str, set] = {}

    async def sismember(self, key, member) -> bool:
        return member in self.sets.get(key, set())

    async def sadd(self, key, member) -> int:
        s = self.sets.setdefault(key, set())
        if member in s:
            return 0
        s.add(member)
        return 1

    async def srem(self, key, member) -> None:
        self.sets.get(key, set()).discard(member)

    async def scard(self, key) -> int:
        return len(self.sets.get(key, set()))

    async def expire(self, key, ttl) -> None:
        pass


class _FakeCache:
    def __init__(self) -> None:
        self.enabled = True
        self.client = _FakeClient()


def _error(cmd) -> str | None:
    """Return the error string from a cap-hit Command, or None if admitted."""
    if cmd is None:
        return None
    payload = json.loads(cmd.update["messages"][0].content)
    assert payload["success"] is False
    return payload["error"]


@pytest.fixture
def cache(monkeypatch):
    c = _FakeCache()
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client", lambda: c
    )
    return c


@pytest.mark.asyncio
async def test_per_flash_cap_rejects_beyond_limit(cache):
    flash, user = "flash-1", "u-1"
    for i in range(T.MAX_DISPATCH_PER_FLASH):
        assert _error(await T._reserve_dispatch_slot(flash, f"p{i}", user, "tc")) is None
    err = await T._reserve_dispatch_slot(flash, "p-over", user, "tc")
    assert _error(err) is not None
    assert str(T.MAX_DISPATCH_PER_FLASH) in _error(err)
    # The rejected dispatch left no residue in either SET.
    assert "p-over" not in cache.client.sets[f"flash_watch:{flash}"]
    assert "p-over" not in cache.client.sets[f"flash_user_pending:{user}"]


@pytest.mark.asyncio
async def test_per_user_cap_spans_multiple_flash_threads(cache):
    user = "u-1"
    # Spread dispatches across flash threads, staying under each per-flash cap
    # (<5 each) but reaching the per-user cap of 10.
    placed = 0
    flash_idx = 0
    while placed < T.MAX_DISPATCH_PER_USER:
        flash = f"flash-{flash_idx}"
        for _ in range(min(T.MAX_DISPATCH_PER_FLASH - 1, T.MAX_DISPATCH_PER_USER - placed)):
            assert _error(await T._reserve_dispatch_slot(flash, f"p{placed}", user, "tc")) is None
            placed += 1
        flash_idx += 1
    # 11th anywhere is rejected by the per-user cap.
    err = await T._reserve_dispatch_slot("flash-new", "p-over", user, "tc")
    assert _error(err) is not None
    assert str(T.MAX_DISPATCH_PER_USER) in _error(err)


@pytest.mark.asyncio
async def test_idempotent_redispatch_does_not_count_against_cap(cache):
    flash, user = "flash-1", "u-1"
    for i in range(T.MAX_DISPATCH_PER_FLASH):
        assert _error(await T._reserve_dispatch_slot(flash, f"p{i}", user, "tc")) is None
    # Re-reserving an existing member (idempotent re-dispatch) is admitted even
    # though the SET is already at the cap.
    assert _error(await T._reserve_dispatch_slot(flash, "p0", user, "tc")) is None


@pytest.mark.asyncio
async def test_release_rolls_back_both_sets(cache):
    flash, user, ptc = "flash-1", "u-1", "p0"
    assert _error(await T._reserve_dispatch_slot(flash, ptc, user, "tc")) is None
    assert ptc in cache.client.sets[f"flash_watch:{flash}"]
    assert ptc in cache.client.sets[f"flash_user_pending:{user}"]

    await T._release_dispatch_slot(flash, ptc, user)

    assert ptc not in cache.client.sets[f"flash_watch:{flash}"]
    assert ptc not in cache.client.sets[f"flash_user_pending:{user}"]
    # A freed slot is reusable.
    assert _error(await T._reserve_dispatch_slot(flash, "p-new", user, "tc")) is None


@pytest.mark.asyncio
async def test_reserve_is_noop_when_cache_disabled(monkeypatch):
    class _Disabled:
        enabled = False
        client = None

    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client", lambda: _Disabled()
    )
    # No Redis -> best-effort admit (never block the dispatch).
    assert await T._reserve_dispatch_slot("f", "p", "u", "tc") is None
