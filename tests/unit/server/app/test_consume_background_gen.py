"""Crash-path cleanup for `_consume_background_gen`.

When a dispatched background generator raises, the except branch tears down the
report-back watch keyed by the *PTC* thread id. Regression: the FLASH_DISPATCH
site (a report-back run) used the flash thread id as the origin key, so a
report-back run that crashed before its terminal handler fired left the durable
watch/pointer alive until TTL and `/status` kept reporting a stale pending run.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.app.threads import _consume_background_gen


async def _crashing_gen():
    raise RuntimeError("kaboom")
    yield  # unreachable — marks this as an async generator


class _FakeClient:
    def __init__(self):
        self.publish = AsyncMock()
        self.xadd = AsyncMock()


class _FakeCache:
    def __init__(self, origin_map):
        self.enabled = True
        self.client = _FakeClient()
        self._origin = origin_map

    async def get(self, key):
        return self._origin.get(key)


def _patched(cache, clear):
    return (
        patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ),
        patch(
            "src.server.handlers.chat.report_back.clear_flash_report_back", clear
        ),
    )


@pytest.mark.asyncio
async def test_report_back_crash_clears_watch_via_ptc_thread_id():
    # report-back run: thread_id is the flash thread, but the origin lives under
    # the completed PTC thread named by report_back_ptc_thread_id.
    cache = _FakeCache({"ptc_origin:ptc-1": {"flash_thread_id": "flash-1"}})
    clear = AsyncMock()
    p1, p2 = _patched(cache, clear)
    with p1, p2:
        ok = await _consume_background_gen(
            _crashing_gen(),
            "FLASH_DISPATCH",
            "flash-1",
            "run-1",
            report_back_ptc_thread_id="ptc-1",
            user_id="user-1",
        )
    assert ok is False
    # The known owner is threaded through so the per-user cap slot is released
    # even when ptc_origin carries no user_id (would TTL-leak otherwise).
    clear.assert_awaited_once_with(cache, "ptc-1", "flash-1", user_id="user-1")
    cache.client.publish.assert_awaited_once()
    assert cache.client.publish.call_args[0][0] == "thread:wake:flash-1"


@pytest.mark.asyncio
async def test_ordinary_flash_dispatch_crash_preserves_watch():
    # No report_back id: the origin lookup uses the flash thread id, misses, and
    # leaves a still-running dispatched PTC's keys intact for reload recovery.
    cache = _FakeCache({})  # ptc_origin:flash-1 absent
    clear = AsyncMock()
    p1, p2 = _patched(cache, clear)
    with p1, p2:
        ok = await _consume_background_gen(
            _crashing_gen(), "FLASH_DISPATCH", "flash-1", "run-1"
        )
    assert ok is False
    clear.assert_not_awaited()
    cache.client.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_ptc_dispatch_crash_clears_via_thread_id():
    # PTC_DISPATCH: thread_id IS the ptc thread, so the default origin key hits.
    cache = _FakeCache({"ptc_origin:ptc-9": {"flash_thread_id": "flash-9"}})
    clear = AsyncMock()
    p1, p2 = _patched(cache, clear)
    with p1, p2:
        ok = await _consume_background_gen(
            _crashing_gen(), "PTC_DISPATCH", "ptc-9", "run-9", user_id="user-9"
        )
    assert ok is False
    clear.assert_awaited_once_with(cache, "ptc-9", "flash-9", user_id="user-9")
    assert cache.client.publish.call_args[0][0] == "thread:wake:flash-9"


@pytest.mark.asyncio
async def test_crash_path_appends_stream_end_sentinel_after_error_event():
    """The dispatch-failure path writes the terminal ``error`` SSE and THEN the
    stream-end sentinel, so an attached consumer closes immediately instead of
    dwelling on the empty-XREAD handshake."""
    from src.server.services.background_task_manager import BackgroundTaskManager

    cache = _FakeCache({})
    order = []
    cache.client.xadd.side_effect = lambda *a, **kw: order.append("error_xadd")

    fake_manager = AsyncMock()
    fake_manager.append_stream_end_sentinel.side_effect = (
        lambda *a, **kw: order.append("sentinel")
    )

    clear = AsyncMock()
    p1, p2 = _patched(cache, clear)
    with p1, p2, patch.object(
        BackgroundTaskManager,
        "get_instance",
        classmethod(lambda cls: fake_manager),
    ):
        ok = await _consume_background_gen(
            _crashing_gen(), "PTC_DISPATCH", "ptc-1", "run-1"
        )

    assert ok is False
    assert order == ["error_xadd", "sentinel"]
    fake_manager.append_stream_end_sentinel.assert_awaited_once_with(
        "ptc-1", "run-1"
    )
