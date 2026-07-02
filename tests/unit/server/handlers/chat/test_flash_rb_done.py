"""Coverage for the recently-drained report-back record (``flash_rb_done``).

``clear_flash_report_back`` deletes the run pointer + watch membership
atomically, so a client that missed the pub/sub wake could never learn a
finished report-back turn's run id. After a successful clear the drained run id
is recorded on a bounded, TTL'd per-flash list that ``read_report_back_status``
surfaces as ``recent_report_back_run_ids`` (newest first) — best-effort, never
a reason to fail the clear, and skipped on the deleted-flash mass discard.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.server.handlers.chat import report_back
from tests.unit.server.handlers.chat.test_flash_report_back import (
    _drain,
    _FakeCache,
    _seed_dispatched,
)


@pytest.fixture(autouse=True)
def _reset_consumer_state():
    """Module-global consumer registries must not leak across tests."""
    report_back._rb_consumers.clear()
    report_back._rb_terminal_events.clear()
    yield
    for task in list(report_back._rb_consumers.values()):
        task.cancel()
    report_back._rb_consumers.clear()
    report_back._rb_terminal_events.clear()


def _seed_pointer(cache: _FakeCache, flash: str, ptc: str, run_id: str) -> None:
    cache.kv[report_back.flash_rb_run_key(flash, ptc)] = {"run_id": run_id}


# ---------------------------------------------------------------------------
# Recording on clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_records_drained_run_newest_first_with_ttl():
    cache = _FakeCache()
    flash = "flash-1"
    _seed_dispatched(cache, flash, ["ptc-1", "ptc-2"])
    _seed_pointer(cache, flash, "ptc-1", "rb-run-1")
    _seed_pointer(cache, flash, "ptc-2", "rb-run-2")

    await report_back.clear_flash_report_back(cache, "ptc-1", flash)
    await report_back.clear_flash_report_back(cache, "ptc-2", flash)

    done_key = report_back.flash_rb_done_key(flash)
    assert cache.client.lists[done_key] == ["rb-run-2", "rb-run-1"]  # newest first
    assert cache.client.ttls[done_key] == report_back._FLASH_RB_DONE_TTL


@pytest.mark.asyncio
async def test_clear_without_run_pointer_records_nothing():
    """No pointer -> nothing was ever dispatched for this pair; nothing to find."""
    cache = _FakeCache()
    flash, ptc = "flash-1", "ptc-1"
    _seed_dispatched(cache, flash, [ptc])

    await report_back.clear_flash_report_back(cache, ptc, flash)

    assert not cache.client.lists.get(report_back.flash_rb_done_key(flash))
    # The teardown itself still ran.
    assert ptc not in cache.client.sets.get(report_back.flash_watch_key(flash), set())
    assert f"ptc_origin:{ptc}" not in cache.kv


@pytest.mark.asyncio
async def test_done_list_bounded_at_max():
    cache = _FakeCache()
    flash = "flash-1"
    count = report_back._FLASH_RB_DONE_MAX + 2
    ptcs = [f"ptc-{i}" for i in range(1, count + 1)]
    _seed_dispatched(cache, flash, ptcs)

    for i, ptc in enumerate(ptcs, 1):
        _seed_pointer(cache, flash, ptc, f"rb-run-{i}")
        await report_back.clear_flash_report_back(cache, ptc, flash)

    done = cache.client.lists[report_back.flash_rb_done_key(flash)]
    assert len(done) == report_back._FLASH_RB_DONE_MAX
    assert done[0] == f"rb-run-{count}"  # newest kept
    assert "rb-run-1" not in done and "rb-run-2" not in done  # oldest trimmed


@pytest.mark.asyncio
async def test_retried_clear_does_not_duplicate_run_id():
    """A re-cleared pair (e.g. the consumer's belt-and-suspenders pointer
    re-assert racing the terminal) LREM-dedups: the run id appears once."""
    cache = _FakeCache()
    flash, ptc = "flash-1", "ptc-1"
    _seed_dispatched(cache, flash, [ptc])
    _seed_pointer(cache, flash, ptc, "rb-run-1")

    await report_back.clear_flash_report_back(cache, ptc, flash)
    # Retry: the pointer was re-asserted after the first clear.
    _seed_pointer(cache, flash, ptc, "rb-run-1")
    await report_back.clear_flash_report_back(cache, ptc, flash)

    assert cache.client.lists[report_back.flash_rb_done_key(flash)] == ["rb-run-1"]


@pytest.mark.asyncio
async def test_recording_failure_does_not_break_clear():
    """Recording is best-effort: a Redis failure there must not fail the clear."""
    cache = _FakeCache()
    flash, ptc = "flash-1", "ptc-1"
    _seed_dispatched(cache, flash, [ptc])
    _seed_pointer(cache, flash, ptc, "rb-run-1")

    async def _boom(key, value):
        raise RuntimeError("redis down")

    cache.client.lpush = _boom

    await report_back.clear_flash_report_back(cache, ptc, flash)  # must not raise

    # Teardown fully applied despite the failed record.
    assert ptc not in cache.client.sets.get(report_back.flash_watch_key(flash), set())
    assert f"ptc_origin:{ptc}" not in cache.kv
    assert report_back.flash_rb_run_key(flash, ptc) not in cache.kv
    assert not cache.client.lists.get(report_back.flash_rb_done_key(flash))


@pytest.mark.asyncio
async def test_mass_discard_of_deleted_flash_thread_records_nothing():
    """A 404'd flash thread can never render these turns; don't advertise them."""
    cache = _FakeCache()
    flash = "flash-1"
    _seed_dispatched(cache, flash, ["ptc-1", "ptc-2"])
    _seed_pointer(cache, flash, "ptc-1", "rb-run-1")
    _seed_pointer(cache, flash, "ptc-2", "rb-run-2")

    await report_back._discard_flash_thread(cache, flash)

    assert not cache.client.lists.get(report_back.flash_rb_done_key(flash))
    assert not cache.client.sets.get(report_back.flash_watch_key(flash))


@pytest.mark.asyncio
async def test_consumer_drain_records_each_run_newest_first():
    """End-to-end: each drained report-back's run id lands on the done list in
    drain order (newest first), mirroring claim-at-admission + terminal clear."""
    cache = _FakeCache()
    flash = "flash-1"
    _seed_dispatched(cache, flash, ["ptc-1", "ptc-2"])

    async def fake_post(c, f, ptc, origin):
        # Admission claims the run pointer; the terminal clear then drains it.
        _seed_pointer(c, f, ptc, f"rb-run-{ptc}")
        await report_back.clear_flash_report_back(c, ptc, f)
        return "dispatched", f"rb-run-{ptc}"

    with patch(
        "src.utils.cache.redis_cache.get_cache_client", return_value=cache
    ), patch.object(report_back, "_post_report_back", side_effect=fake_post):
        await report_back._flash_report_back("ptc-1")
        await report_back._flash_report_back("ptc-2")
        await _drain(flash)

    done = cache.client.lists[report_back.flash_rb_done_key(flash)]
    assert done == ["rb-run-ptc-2", "rb-run-ptc-1"]


# ---------------------------------------------------------------------------
# read_report_back_status surfaces the record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_surfaces_recent_run_ids_newest_first():
    cache = _FakeCache()
    flash = "flash-1"
    cache.client.lists[report_back.flash_rb_done_key(flash)] = ["rb-run-2", "rb-run-1"]

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache):
        resp = await report_back.read_report_back_status(flash)

    assert resp["recent_report_back_run_ids"] == ["rb-run-2", "rb-run-1"]
    # Drained thread: no live members, but the recent list is still served.
    assert resp["pending_report_back"] is False
    assert resp["report_back_run_id"] is None


@pytest.mark.asyncio
async def test_status_recent_run_ids_empty_when_key_absent():
    cache = _FakeCache()

    with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache):
        resp = await report_back.read_report_back_status("flash-1")

    assert resp["recent_report_back_run_ids"] == []
