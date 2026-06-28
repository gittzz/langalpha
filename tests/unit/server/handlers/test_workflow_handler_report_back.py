"""Coverage for ``get_workflow_status``'s flash report-back resolution.

A flash thread polling ``/status`` must surface which report-back run to attach
to (``report_back_run_id``) and re-nudge the in-process consumer after a process
restart. The run is resolved from a live per-(flash, ptc) pointer — preferring
the head of the durable queue (the report-back currently being drained), then
any pending watch member with a pointer.
"""

from __future__ import annotations

import contextlib

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.server.handlers import workflow_handler
from src.server.handlers.chat import ptc_workflow


class _FakeClient:
    def __init__(self) -> None:
        self.sets: dict[str, set] = {}
        self.lists: dict[str, list] = {}

    async def scard(self, key) -> int:
        return len(self.sets.get(key, set()))

    async def lindex(self, key, index):
        lst = self.lists.get(key, [])
        return lst[index] if -len(lst) <= index < len(lst) else None

    async def smembers(self, key) -> set:
        return set(self.sets.get(key, set()))

    async def llen(self, key) -> int:
        return len(self.lists.get(key, []))


class _FakeCache:
    def __init__(self) -> None:
        self.enabled = True
        self.client = _FakeClient()
        self.kv: dict[str, object] = {}

    async def get(self, key):
        return self.kv.get(key)


def _seed(cache: _FakeCache, flash: str, queue: list[str], run_pointers: dict[str, str]) -> None:
    cache.client.sets[ptc_workflow.flash_watch_key(flash)] = set(queue)
    cache.client.lists[ptc_workflow.flash_rb_queue_key(flash)] = list(queue)
    for ptc, run_id in run_pointers.items():
        cache.kv[ptc_workflow.flash_rb_run_key(flash, ptc)] = {"run_id": run_id}


def _patches(cache: _FakeCache, ensure: MagicMock) -> list:
    """Stub everything get_workflow_status touches except the report-back block."""
    tracker = MagicMock()
    # COMPLETED is terminal (not reconnectable) -> can_reconnect False.
    tracker.get_status = AsyncMock(
        return_value={
            "status": "completed",
            "last_update": None,
            "workspace_id": "ws-1",
            "user_id": "u-1",
        }
    )
    manager = MagicMock()
    # A "found" bg status skips the stale-clear branch regardless of can_reconnect.
    manager.get_workflow_status = AsyncMock(
        return_value={"status": "running", "active_tasks": [], "run_id": "r1"}
    )
    return [
        patch.object(
            workflow_handler, "get_checkpoint_tuple", AsyncMock(return_value=None)
        ),
        patch(
            "src.server.services.workflow_tracker.WorkflowTracker.get_instance",
            return_value=tracker,
        ),
        patch(
            "src.server.services.background_task_manager.BackgroundTaskManager.get_instance",
            return_value=manager,
        ),
        patch(
            "src.server.database.conversation.get_thread_by_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ),
        patch.object(ptc_workflow, "ensure_rb_consumer", ensure),
    ]


@pytest.mark.asyncio
async def test_status_prefers_queue_head_run_id_and_nudges_consumer():
    cache = _FakeCache()
    flash = "flash-1"
    # Both head and member carry a pointer; the head's must win.
    _seed(
        cache,
        flash,
        ["ptc-head", "ptc-other"],
        {"ptc-head": "rb-head", "ptc-other": "rb-other"},
    )
    ensure = MagicMock()

    with contextlib.ExitStack() as stack:
        for p in _patches(cache, ensure):
            stack.enter_context(p)
        resp = await workflow_handler.get_workflow_status(flash)

    assert resp["pending_report_back"] is True
    assert resp["report_back_run_id"] == "rb-head"
    # Durable queue is non-empty -> restart-nudge the consumer.
    ensure.assert_called_once_with(flash)


@pytest.mark.asyncio
async def test_status_falls_back_to_member_pointer_when_head_has_none():
    cache = _FakeCache()
    flash = "flash-1"
    # Head has no pointer yet; resolution falls through to the member that does.
    _seed(cache, flash, ["ptc-head", "ptc-other"], {"ptc-other": "rb-other"})
    ensure = MagicMock()

    with contextlib.ExitStack() as stack:
        for p in _patches(cache, ensure):
            stack.enter_context(p)
        resp = await workflow_handler.get_workflow_status(flash)

    assert resp["pending_report_back"] is True
    assert resp["report_back_run_id"] == "rb-other"
    ensure.assert_called_once_with(flash)


@pytest.mark.asyncio
async def test_status_no_pending_report_back_does_not_nudge():
    cache = _FakeCache()  # empty watch SET + empty queue
    ensure = MagicMock()

    with contextlib.ExitStack() as stack:
        for p in _patches(cache, ensure):
            stack.enter_context(p)
        resp = await workflow_handler.get_workflow_status("flash-x")

    assert resp["pending_report_back"] is False
    assert resp["report_back_run_id"] is None
    ensure.assert_not_called()
