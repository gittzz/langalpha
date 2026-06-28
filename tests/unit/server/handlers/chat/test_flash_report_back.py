"""Tests for the flash report-back serialization path (N-concurrent dispatch).

A flash thread can dispatch many background PTC analyses; each completion must
render back as its own ordered, non-overlapping turn. The machinery:

- ``_flash_report_back`` is enqueue-only — it appends the completed PTC to the
  flash thread's durable FIFO (``flash_rb_queue``) and starts the in-process
  consumer. Deduped against a duplicate (at-least-once) completion event.
- ``_rb_consumer_loop`` / ``_drain_one_report_back`` POST one report-back as a
  turn and await its terminal (per-(flash, ptc) ``asyncio.Event``, set by
  ``clear_flash_report_back``) before advancing — so completion order is
  preserved and turns never overlap.
- ``clear_flash_report_back`` tears down all per-pair state and wakes the
  consumer. The flash completion hook calls it only when
  ``report_back_ptc_thread_id`` is set.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.server.handlers.chat import flash_workflow, ptc_workflow


# ---------------------------------------------------------------------------
# Stateful fake Redis — models the SET / LIST / KV ops the path actually uses,
# so the consumer's keep-until-terminal queue draining is exercised for real.
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Queues client ops and replays them against the same fake client on execute.

    Mirrors redis-py's async pipeline shape: command methods are synchronous
    (queue + return self), ``execute`` is awaited and runs them in order.
    """

    def __init__(self, client: "_FakeClient") -> None:
        self._client = client
        self._ops: list = []

    def delete(self, key) -> "_FakePipeline":
        self._ops.append(("delete", key))
        return self

    def srem(self, key, member) -> "_FakePipeline":
        self._ops.append(("srem", key, member))
        return self

    def sadd(self, key, member) -> "_FakePipeline":
        self._ops.append(("sadd", key, member))
        return self

    def lrem(self, key, count, value) -> "_FakePipeline":
        self._ops.append(("lrem", key, count, value))
        return self

    def expire(self, key, ttl) -> "_FakePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> list:
        results = []
        for name, *args in self._ops:
            results.append(await getattr(self._client, name)(*args))
        self._ops.clear()
        return results


class _FakeClient:
    def __init__(self) -> None:
        self.sets: dict[str, set] = {}
        self.lists: dict[str, list] = {}
        # Shared with _FakeCache.kv so raw-client DELETE (pipeline) and the
        # wrapper's get/set/delete address one keyspace, as real Redis does.
        self.kv: dict[str, object] = {}
        self.published: list[tuple[str, str]] = []

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

    async def smembers(self, key) -> set:
        return set(self.sets.get(key, set()))

    async def rpush(self, key, value) -> None:
        self.lists.setdefault(key, []).append(value)

    async def lindex(self, key, index):
        lst = self.lists.get(key, [])
        return lst[index] if -len(lst) <= index < len(lst) else None

    async def lrem(self, key, count, value) -> None:
        lst = self.lists.get(key, [])
        if count == 0:
            self.lists[key] = [x for x in lst if x != value]
            return
        removed = 0
        out = []
        for x in lst:
            if x == value and removed < count:
                removed += 1
                continue
            out.append(x)
        self.lists[key] = out

    async def llen(self, key) -> int:
        return len(self.lists.get(key, []))

    async def expire(self, key, ttl) -> None:
        pass

    async def delete(self, key) -> None:
        self.sets.pop(key, None)
        self.lists.pop(key, None)
        self.kv.pop(key, None)

    async def publish(self, channel, message) -> None:
        self.published.append((channel, message))

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


class _FakeCache:
    def __init__(self) -> None:
        self.enabled = True
        self.client = _FakeClient()
        # One keyspace: the wrapper's string KV is the client's kv dict, so a
        # pipeline DELETE on the raw client removes wrapper-set keys too.
        self.kv: dict[str, object] = self.client.kv

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ttl=None) -> None:
        self.kv[key] = value

    async def delete(self, key) -> None:
        self.kv.pop(key, None)


def _origin(ptc: str, flash: str = "flash-1", user: str = "u-1") -> dict:
    return {
        "origin": "flash",
        "report_back": True,
        "flash_thread_id": flash,
        "flash_workspace_id": "fws-1",
        "ptc_thread_id": ptc,
        "ptc_workspace_id": f"ws-{ptc}",
        "user_id": user,
    }


def _seed_dispatched(cache: _FakeCache, flash: str, ptcs: list[str], user: str = "u-1") -> None:
    """Mirror what reservation + origin recording leave behind for each dispatch."""
    for ptc in ptcs:
        cache.client.sets.setdefault(ptc_workflow.flash_watch_key(flash), set()).add(ptc)
        cache.client.sets.setdefault(f"flash_user_pending:{user}", set()).add(ptc)
        cache.kv[f"ptc_origin:{ptc}"] = _origin(ptc, flash, user)


async def _drain(flash: str, *, ticks: int = 200) -> None:
    """Yield to the event loop until the flash consumer task finishes."""
    for _ in range(ticks):
        await asyncio.sleep(0)
        task = ptc_workflow._rb_consumers.get(flash)
        if task is not None and task.done():
            return
    task = ptc_workflow._rb_consumers.get(flash)
    if task is not None:
        await asyncio.wait_for(task, timeout=2.0)


@pytest.fixture(autouse=True)
def _reset_consumer_state():
    """Module-global consumer registries must not leak across tests."""
    ptc_workflow._rb_consumers.clear()
    ptc_workflow._rb_terminal_events.clear()
    yield
    for task in list(ptc_workflow._rb_consumers.values()):
        task.cancel()
    ptc_workflow._rb_consumers.clear()
    ptc_workflow._rb_terminal_events.clear()


# ---------------------------------------------------------------------------
# clear_flash_report_back — full per-pair teardown + consumer wake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_tears_down_all_per_pair_state_and_sets_event():
    cache = _FakeCache()
    flash, ptc = "flash-1", "ptc-1"
    _seed_dispatched(cache, flash, [ptc])
    await cache.client.rpush(ptc_workflow.flash_rb_queue_key(flash), ptc)
    await cache.client.sadd(ptc_workflow.flash_rb_queued_key(flash), ptc)
    cache.kv[ptc_workflow.flash_rb_run_key(flash, ptc)] = {"run_id": "rb-1"}
    event = asyncio.Event()
    ptc_workflow._rb_terminal_events[(flash, ptc)] = event

    await ptc_workflow.clear_flash_report_back(cache, ptc, flash)

    assert f"ptc_origin:{ptc}" not in cache.kv
    assert ptc_workflow.flash_rb_run_key(flash, ptc) not in cache.kv
    assert ptc not in cache.client.sets.get(ptc_workflow.flash_watch_key(flash), set())
    assert ptc not in cache.client.sets.get("flash_user_pending:u-1", set())
    assert ptc not in cache.client.lists.get(ptc_workflow.flash_rb_queue_key(flash), [])
    assert ptc not in cache.client.sets.get(ptc_workflow.flash_rb_queued_key(flash), set())
    assert event.is_set()  # consumer waiting on this pair is woken


@pytest.mark.asyncio
async def test_clear_without_flash_thread_id_only_deletes_origin():
    cache = _FakeCache()
    cache.kv["ptc_origin:ptc-1"] = _origin("ptc-1")

    await ptc_workflow.clear_flash_report_back(cache, "ptc-1", None)

    assert "ptc_origin:ptc-1" not in cache.kv


@pytest.mark.asyncio
async def test_clear_runs_all_six_mutations_in_one_pipeline():
    """The teardown must be all-or-nothing (single transaction) so a partial
    failure can't leak the per-user cap."""
    cache = _FakeCache()
    flash, ptc = "flash-1", "ptc-1"
    _seed_dispatched(cache, flash, [ptc])
    await cache.client.rpush(ptc_workflow.flash_rb_queue_key(flash), ptc)
    await cache.client.sadd(ptc_workflow.flash_rb_queued_key(flash), ptc)
    cache.kv[ptc_workflow.flash_rb_run_key(flash, ptc)] = {"run_id": "rb-1"}

    executes = 0
    orig_pipeline = cache.client.pipeline

    def _counting_pipeline(transaction: bool = True):
        pipe = orig_pipeline(transaction=transaction)
        orig_execute = pipe.execute

        async def _execute():
            nonlocal executes
            executes += 1
            return await orig_execute()

        pipe.execute = _execute
        return pipe

    cache.client.pipeline = _counting_pipeline

    await ptc_workflow.clear_flash_report_back(cache, ptc, flash)

    assert executes == 1  # all six mutations issued in one transaction
    # All six keys are gone.
    assert f"ptc_origin:{ptc}" not in cache.kv
    assert ptc_workflow.flash_rb_run_key(flash, ptc) not in cache.kv
    assert ptc not in cache.client.sets.get(ptc_workflow.flash_watch_key(flash), set())
    assert ptc not in cache.client.sets.get("flash_user_pending:u-1", set())
    assert ptc not in cache.client.lists.get(ptc_workflow.flash_rb_queue_key(flash), [])
    assert ptc not in cache.client.sets.get(ptc_workflow.flash_rb_queued_key(flash), set())


# ---------------------------------------------------------------------------
# _flash_report_back — enqueue-only + dedup + gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_appends_once_and_dedups_duplicate_completion():
    cache = _FakeCache()
    flash, ptc = "flash-1", "ptc-1"
    _seed_dispatched(cache, flash, [ptc])

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache), patch.object(
        ptc_workflow, "ensure_rb_consumer"
    ) as ensure:
        await ptc_workflow._flash_report_back(ptc, "ws-1")
        await ptc_workflow._flash_report_back(ptc, "ws-1")  # at-least-once duplicate

    assert cache.client.lists[ptc_workflow.flash_rb_queue_key(flash)] == [ptc]
    # The duplicate completion returns at the dedup gate, before re-nudging.
    assert ensure.call_count == 1


@pytest.mark.asyncio
async def test_enqueue_skips_non_member_and_non_report_back():
    cache = _FakeCache()
    flash = "flash-1"
    # origin present but PTC was never a watch member (cap rollback / already cleared)
    cache.kv["ptc_origin:ptc-gone"] = _origin("ptc-gone", flash)
    # origin present, report_back disabled
    cache.kv["ptc_origin:ptc-noflag"] = _origin("ptc-noflag", flash)
    cache.kv["ptc_origin:ptc-noflag"]["report_back"] = False
    cache.client.sets[ptc_workflow.flash_watch_key(flash)] = {"ptc-noflag"}

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache), patch.object(
        ptc_workflow, "ensure_rb_consumer"
    ) as ensure:
        await ptc_workflow._flash_report_back("ptc-gone", "ws-1")
        await ptc_workflow._flash_report_back("ptc-noflag", "ws-1")

    assert ptc_workflow.flash_rb_queue_key(flash) not in cache.client.lists
    ensure.assert_not_called()


# ---------------------------------------------------------------------------
# Consumer — ordered, non-overlapping drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_drains_in_completion_order_at_n():
    cache = _FakeCache()
    flash = "flash-1"
    ptcs = ["ptc-a", "ptc-b", "ptc-c", "ptc-d"]
    _seed_dispatched(cache, flash, ptcs)
    order: list[str] = []

    async def fake_post(c, f, ptc, origin):
        order.append(ptc)
        await ptc_workflow.clear_flash_report_back(c, ptc, f)  # terminal "immediately"
        return "dispatched", f"run-{ptc}"

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache), patch.object(
        ptc_workflow, "_post_report_back", side_effect=fake_post
    ):
        # completion order deliberately != lexical order
        for ptc in ["ptc-c", "ptc-a", "ptc-d", "ptc-b"]:
            await ptc_workflow._flash_report_back(ptc, "ws-1")
        await _drain(flash)

    assert order == ["ptc-c", "ptc-a", "ptc-d", "ptc-b"]
    assert not cache.client.sets.get(ptc_workflow.flash_watch_key(flash))
    assert not cache.client.lists.get(ptc_workflow.flash_rb_queue_key(flash))
    assert not cache.client.sets.get(ptc_workflow.flash_rb_queued_key(flash))


@pytest.mark.asyncio
async def test_consumer_waits_for_terminal_before_advancing():
    cache = _FakeCache()
    flash = "flash-1"
    _seed_dispatched(cache, flash, ["ptc-1", "ptc-2"])
    order: list[str] = []

    async def fake_post(c, f, ptc, origin):
        order.append(ptc)
        return "dispatched", f"run-{ptc}"  # NO terminal — consumer must block

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache), patch.object(
        ptc_workflow, "_post_report_back", side_effect=fake_post
    ):
        await ptc_workflow._flash_report_back("ptc-1", "ws-1")
        await ptc_workflow._flash_report_back("ptc-2", "ws-1")
        for _ in range(20):
            await asyncio.sleep(0)
        # Parked on ptc-1's terminal — ptc-2 not yet POSTed.
        assert order == ["ptc-1"]

        await ptc_workflow.clear_flash_report_back(cache, "ptc-1", flash)  # ptc-1 done
        for _ in range(20):
            await asyncio.sleep(0)
        assert order == ["ptc-1", "ptc-2"]

        await ptc_workflow.clear_flash_report_back(cache, "ptc-2", flash)
        await _drain(flash)

    assert not cache.client.lists.get(ptc_workflow.flash_rb_queue_key(flash))


@pytest.mark.asyncio
async def test_consumer_skips_stale_head_whose_membership_is_gone():
    cache = _FakeCache()
    flash = "flash-1"
    _seed_dispatched(cache, flash, ["ptc-2"])  # only ptc-2 is a live member
    order: list[str] = []

    # ptc-1 sits at the queue head but its membership was already cleared.
    await cache.client.rpush(ptc_workflow.flash_rb_queue_key(flash), "ptc-1")
    await cache.client.sadd(ptc_workflow.flash_rb_queued_key(flash), "ptc-1")

    async def fake_post(c, f, ptc, origin):
        order.append(ptc)
        await ptc_workflow.clear_flash_report_back(c, ptc, f)
        return "dispatched", f"run-{ptc}"

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache), patch.object(
        ptc_workflow, "_post_report_back", side_effect=fake_post
    ):
        await ptc_workflow._flash_report_back("ptc-2", "ws-1")
        await _drain(flash)

    assert order == ["ptc-2"]  # stale ptc-1 skipped, never POSTed
    assert "ptc-1" not in cache.client.lists.get(ptc_workflow.flash_rb_queue_key(flash), [])


@pytest.mark.asyncio
async def test_consumer_drops_member_on_permanent_rejection_and_advances():
    cache = _FakeCache()
    flash = "flash-1"
    _seed_dispatched(cache, flash, ["ptc-1", "ptc-2"])
    order: list[str] = []

    async def fake_post(c, f, ptc, origin):
        order.append(ptc)
        if ptc == "ptc-1":
            return "drop", None  # permanent 4xx — no run created
        await ptc_workflow.clear_flash_report_back(c, ptc, f)
        return "dispatched", f"run-{ptc}"

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache), patch.object(
        ptc_workflow, "_post_report_back", side_effect=fake_post
    ):
        await ptc_workflow._flash_report_back("ptc-1", "ws-1")
        await ptc_workflow._flash_report_back("ptc-2", "ws-1")
        await _drain(flash)

    assert order == ["ptc-1", "ptc-2"]  # dropped ptc-1 still advanced to ptc-2
    assert not cache.client.sets.get(ptc_workflow.flash_watch_key(flash))
    assert not cache.client.lists.get(ptc_workflow.flash_rb_queue_key(flash))


@pytest.mark.asyncio
async def test_consumer_bounds_terminal_wait_and_force_clears_stuck_member(monkeypatch):
    """A report-back that POSTs but never reaches terminal (e.g. its run crashed)
    must not wedge the queue: a hard deadline force-clears it so the consumer
    advances."""
    monkeypatch.setattr(ptc_workflow, "_RB_TERMINAL_WAIT_CAP", 0.0)
    cache = _FakeCache()
    flash = "flash-1"
    _seed_dispatched(cache, flash, ["ptc-1", "ptc-2"])
    order: list[str] = []

    async def fake_post(c, f, ptc, origin):
        order.append(ptc)
        return "dispatched", f"run-{ptc}"  # NO terminal ever fires

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache), patch.object(
        ptc_workflow, "_post_report_back", side_effect=fake_post
    ):
        await ptc_workflow._flash_report_back("ptc-1", "ws-1")
        await ptc_workflow._flash_report_back("ptc-2", "ws-1")
        await _drain(flash)

    # Deadline force-cleared each stuck member, so the consumer drained the whole
    # queue instead of hanging on ptc-1 forever.
    assert order == ["ptc-1", "ptc-2"]
    assert not cache.client.sets.get(ptc_workflow.flash_watch_key(flash))
    assert not cache.client.lists.get(ptc_workflow.flash_rb_queue_key(flash))
    assert not cache.client.sets.get(ptc_workflow.flash_rb_queued_key(flash))


@pytest.mark.asyncio
async def test_consumer_discards_whole_thread_on_404():
    cache = _FakeCache()
    flash = "flash-1"
    _seed_dispatched(cache, flash, ["ptc-1", "ptc-2", "ptc-3"])
    order: list[str] = []

    async def fake_post(c, f, ptc, origin):
        order.append(ptc)
        return "deleted", None  # flash thread gone

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache), patch.object(
        ptc_workflow, "_post_report_back", side_effect=fake_post
    ):
        await ptc_workflow._flash_report_back("ptc-1", "ws-1")
        await ptc_workflow._flash_report_back("ptc-2", "ws-1")
        await ptc_workflow._flash_report_back("ptc-3", "ws-1")
        await _drain(flash)

    assert order == ["ptc-1"]  # first 404 discards the rest without POSTing
    assert not cache.client.sets.get(ptc_workflow.flash_watch_key(flash))
    assert ptc_workflow.flash_rb_queue_key(flash) not in cache.client.lists
    assert ptc_workflow.flash_rb_queued_key(flash) not in cache.client.lists
    for ptc in ["ptc-1", "ptc-2", "ptc-3"]:
        assert f"ptc_origin:{ptc}" not in cache.kv


# ---------------------------------------------------------------------------
# Flash completion hook -> clear gated on report_back_ptc_thread_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_clears_when_report_back_id_set():
    cache = _FakeCache()
    request = SimpleNamespace(report_back_ptc_thread_id="ptc-1")

    with patch(
        "src.utils.cache.redis_cache.get_cache_client", return_value=cache
    ), patch(
        "src.server.handlers.chat.ptc_workflow.clear_flash_report_back",
        new=AsyncMock(),
    ) as mock_clear:
        await flash_workflow._maybe_clear_report_back(request, "flash-1")

    mock_clear.assert_awaited_once_with(cache, "ptc-1", "flash-1")


@pytest.mark.asyncio
async def test_completion_skips_clear_when_report_back_id_none():
    request = SimpleNamespace(report_back_ptc_thread_id=None)

    with patch(
        "src.server.handlers.chat.ptc_workflow.clear_flash_report_back",
        new=AsyncMock(),
    ) as mock_clear, patch(
        "src.utils.cache.redis_cache.get_cache_client"
    ) as mock_get_cache:
        await flash_workflow._maybe_clear_report_back(request, "flash-1")

    mock_clear.assert_not_called()
    mock_get_cache.assert_not_called()  # short-circuits before touching the cache
