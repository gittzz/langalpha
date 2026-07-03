"""Idempotent report-back run claim/release (server-side dedup of a retried POST).

``claim_report_back_run`` is the server-side guard that closes the report-back
double-deliver: a lost-response retry (or a drain re-POST after a crash) must NOT
start a second summary run. It SET-NX's the per-(flash, ptc) run pointer; a prior
admission's pointer makes the retry return that run instead of a new one.

The fake faithfully models the contract the helper depends on: the RAW client
``set(nx=...)`` writes a JSON string (returns True/None), and the WRAPPER ``get``
json-decodes it — exactly how RedisCache splits raw writes from decoded reads.
"""

from __future__ import annotations

import json

import pytest

from src.server.handlers.chat.report_back import (
    claim_report_back_run,
    flash_rb_run_key,
    release_report_back_run,
)


class _Cache:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.client = self if enabled else None
        self.kv: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        # Raw-client semantics: NX returns None when the key already exists.
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        raw = self.kv.get(key)
        if raw is None:
            return None
        return json.loads(raw) if isinstance(raw, (str, bytes)) else raw

    async def delete(self, key):
        self.kv.pop(key, None)


@pytest.mark.asyncio
async def test_claim_when_no_incumbent_claims_and_writes_pointer():
    cache = _Cache()
    won, claimed = await claim_report_back_run(cache, "flash-1", "ptc-1", "run-1")
    assert (won, claimed) == ("run-1", True)
    # Pointer persisted in the shape the drain gate reads ({"run_id": ...}).
    stored = json.loads(cache.kv[flash_rb_run_key("flash-1", "ptc-1")])
    assert stored == {"run_id": "run-1"}


@pytest.mark.asyncio
async def test_claim_when_incumbent_returns_incumbent_without_overwrite():
    cache = _Cache()
    # A prior admission already owns this (flash, ptc).
    cache.kv[flash_rb_run_key("flash-1", "ptc-1")] = json.dumps({"run_id": "run-A"})
    won, claimed = await claim_report_back_run(cache, "flash-1", "ptc-1", "run-B")
    assert (won, claimed) == ("run-A", False)
    # NX did not overwrite the incumbent.
    stored = json.loads(cache.kv[flash_rb_run_key("flash-1", "ptc-1")])
    assert stored == {"run_id": "run-A"}


class _RacingCache(_Cache):
    """SET NX reports the key exists (falsy) yet the follow-up GET returns None.

    Models a racing delete / degraded read between the NX write and the incumbent
    read: NX loses (the incumbent is "present"), but the incumbent can no longer
    be read — the documented fail-open input.
    """

    async def set(self, key, value, nx=False, ex=None):
        # NX loses unconditionally (incumbent reported present).
        return None

    async def get(self, key):
        # Incumbent unreadable (raced away / degraded read).
        return None


@pytest.mark.asyncio
async def test_claim_fails_open_when_incumbent_unreadable():
    """NX says the pointer exists but the incumbent read returns None -> claim
    degrades to (run_id, True) so the dispatch still proceeds (a lost-incumbent
    read must not stall a completed analysis at the admission gate)."""
    cache = _RacingCache()
    won, claimed = await claim_report_back_run(cache, "flash-1", "ptc-1", "run-1")
    assert (won, claimed) == ("run-1", True)
    # Nothing persisted — we degraded to claimed without writing a bogus pointer.
    assert cache.kv == {}


@pytest.mark.asyncio
async def test_claim_when_cache_disabled_proceeds_without_write():
    cache = _Cache(enabled=False)
    won, claimed = await claim_report_back_run(cache, "flash-1", "ptc-1", "run-1")
    # No idempotency available, but the dispatch must still proceed.
    assert (won, claimed) == ("run-1", True)
    assert cache.kv == {}


@pytest.mark.asyncio
async def test_release_deletes_only_when_pointer_is_ours():
    cache = _Cache()
    key = flash_rb_run_key("flash-1", "ptc-1")
    cache.kv[key] = json.dumps({"run_id": "run-A"})

    # A release for a different run must not delete someone else's pointer.
    await release_report_back_run(cache, "flash-1", "ptc-1", "run-B")
    assert key in cache.kv

    # Our own release removes it (so a later retry isn't short-circuited).
    await release_report_back_run(cache, "flash-1", "ptc-1", "run-A")
    assert key not in cache.kv
