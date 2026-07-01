"""Tests for the workflow_handler cancel path (signal-only + safety net).

Covers decision 1A/1C:
- cancel_workflow is signal-only when a task is active (the except-handler
  teardown owns cancel_and_clear).
- cancel_workflow runs the safety-net cancel_and_clear only when NO active
  task exists (orphaned registry after a crash).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _patch_common(
    *,
    manager_cancel_returns: bool,
    has_active_returns: bool = False,
    manual_compaction_returns: bool = False,
):
    """Patch the collaborators of workflow_handler.cancel_workflow.

    Returns the patch list plus the mocked registry_store, manager, tracker,
    and update_thread_status so tests can assert on the cancel paths.

    ``manual_compaction_returns`` drives ``manager.cancel_compaction`` — when a
    manual /compact is in flight (and no workflow is active) the handler stops
    that compaction and returns early, skipping the workflow-cancel writes.
    """
    tracker = MagicMock()
    tracker.enabled = True
    tracker.set_cancel_flag = AsyncMock(return_value=True)
    tracker.mark_cancelled = AsyncMock(return_value=True)
    # Idle by default (no tracked turn): the idle-thread guard only writes the
    # 'cancelled' status when a turn is genuinely active. Tests that need an
    # active dispatched turn override get_status.
    tracker.get_status = AsyncMock(return_value=None)

    manager = MagicMock()
    manager.cancel_workflow = AsyncMock(return_value=manager_cancel_returns)
    manager.has_active_task_for_thread = AsyncMock(return_value=has_active_returns)
    manager.cancel_compaction = MagicMock(return_value=manual_compaction_returns)

    registry_store = MagicMock()
    registry_store.cancel_and_clear = AsyncMock(return_value=0)

    update_status = AsyncMock()
    patches = [
        patch(
            "src.server.services.workflow_tracker.WorkflowTracker.get_instance",
            return_value=tracker,
        ),
        patch(
            "src.server.services.background_task_manager.BackgroundTaskManager.get_instance",
            return_value=manager,
        ),
        patch(
            "src.server.services.background_registry_store.BackgroundRegistryStore.get_instance",
            return_value=registry_store,
        ),
        patch(
            "src.server.database.conversation.update_thread_status",
            new=update_status,
        ),
    ]
    return patches, registry_store, manager, tracker, update_status


@pytest.mark.asyncio
async def test_cancel_with_active_task_is_signal_only():
    """When a task is active (manager.cancel_workflow → True), the handler must
    NOT call cancel_and_clear — the except-handler teardown owns it."""
    from src.server.handlers.workflow_handler import cancel_workflow

    patches, registry_store, _manager, _tracker, _update = _patch_common(
        manager_cancel_returns=True
    )
    for p in patches:
        p.start()
    try:
        result = await cancel_workflow("t-1")
    finally:
        for p in patches:
            p.stop()

    assert result["cancelled"] is True
    registry_store.cancel_and_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_with_no_active_task_runs_safety_net():
    """No active task (manager.cancel_workflow → False, none active) ⇒ the
    safety-net cancel_and_clear runs to wipe any orphaned registry."""
    from src.server.handlers.workflow_handler import cancel_workflow

    patches, registry_store, _manager, _tracker, _update = _patch_common(
        manager_cancel_returns=False, has_active_returns=False
    )
    for p in patches:
        p.start()
    try:
        result = await cancel_workflow("t-1")
    finally:
        for p in patches:
            p.stop()

    assert result["cancelled"] is True
    registry_store.cancel_and_clear.assert_awaited_once_with("t-1", force=True)


@pytest.mark.asyncio
async def test_run_targeted_miss_with_other_active_task_skips_safety_net():
    """A run-targeted cancel that misses its run (manager.cancel_workflow →
    False) but where ANOTHER turn is still active must NOT wipe the registry —
    that would kill the other turn's subagents."""
    from src.server.handlers.workflow_handler import cancel_workflow

    patches, registry_store, manager, _tracker, _update = _patch_common(
        manager_cancel_returns=False, has_active_returns=True
    )
    for p in patches:
        p.start()
    try:
        result = await cancel_workflow("t-1", "run-A")
    finally:
        for p in patches:
            p.stop()

    assert result["cancelled"] is True
    # run_id threaded through to the manager so it targets the stopped run.
    manager.cancel_workflow.assert_awaited_once_with("t-1", "run-A")
    # Another turn owns the thread → safety net must be skipped.
    registry_store.cancel_and_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_stops_manual_compaction_when_no_active_workflow():
    """A user Stop during a MANUAL compaction (no active workflow) cancels the
    in-flight compaction and returns early — it must NOT run the workflow-cancel
    writes (cancel flag / "cancelled" thread status) that mislabel the thread as
    a stopped turn."""
    from src.server.handlers.workflow_handler import cancel_workflow

    patches, registry_store, manager, tracker, update_status = _patch_common(
        manager_cancel_returns=False,
        has_active_returns=False,
        manual_compaction_returns=True,
    )
    for p in patches:
        p.start()
    try:
        result = await cancel_workflow("t-1")
    finally:
        for p in patches:
            p.stop()

    assert result["cancelled"] is True
    assert result["message"] == "Compaction stopped."
    manager.cancel_compaction.assert_called_once_with("t-1")
    # Early return: none of the workflow-cancel machinery runs.
    manager.cancel_workflow.assert_not_awaited()
    tracker.set_cancel_flag.assert_not_awaited()
    update_status.assert_not_awaited()
    registry_store.cancel_and_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_idle_thread_does_not_mislabel_successful_compaction():
    """A /cancel that lands on an idle thread (no BTM task, no in-flight
    compaction, tracker reports no active turn) — e.g. a Stop click racing a
    compaction that JUST finished — must NOT write a 'cancelled' status (which
    would mislabel the successful compaction as a stopped turn), but must still
    run the orphan-registry safety net."""
    from src.server.handlers.workflow_handler import cancel_workflow

    patches, registry_store, manager, tracker, update_status = _patch_common(
        manager_cancel_returns=False,
        has_active_returns=False,
        manual_compaction_returns=False,  # compaction already finished/cleared
    )
    # tracker reachable, reports no active turn (get_status None by default)
    for p in patches:
        p.start()
    try:
        result = await cancel_workflow("t-1")
    finally:
        for p in patches:
            p.stop()

    assert result["cancelled"] is True
    # No mislabel: the status-mutating writes are skipped on an idle thread.
    tracker.set_cancel_flag.assert_not_awaited()
    tracker.mark_cancelled.assert_not_awaited()
    update_status.assert_not_awaited()
    # Orphan-registry safety net still runs.
    registry_store.cancel_and_clear.assert_awaited_once_with("t-1", force=True)


@pytest.mark.asyncio
async def test_cancel_redis_active_turn_still_writes_cancelled():
    """A dispatched/background turn tracked only in Redis (no BTM task, tracker
    reports ACTIVE) must still get the cancel flag + 'cancelled' status — the
    idle-thread guard only suppresses writes when the tracker confirms idle."""
    from src.server.handlers.workflow_handler import cancel_workflow
    from src.server.services.workflow_tracker import WorkflowStatus

    patches, registry_store, manager, tracker, update_status = _patch_common(
        manager_cancel_returns=True,
        has_active_returns=False,
    )
    tracker.get_status = AsyncMock(return_value={"status": WorkflowStatus.ACTIVE})
    for p in patches:
        p.start()
    try:
        result = await cancel_workflow("t-1")
    finally:
        for p in patches:
            p.stop()

    assert result["cancelled"] is True
    tracker.set_cancel_flag.assert_awaited_once_with("t-1")
    tracker.mark_cancelled.assert_awaited_once_with("t-1")
    update_status.assert_awaited_once_with("t-1", "cancelled")


@pytest.mark.asyncio
async def test_active_workflow_skips_compaction_cancel_shortcircuit():
    """When a workflow is active (auto compaction runs inside the turn), the
    handler must NOT take the manual-compaction shortcut — the normal
    workflow-cancel path interrupts the turn (and its in-flight summarize)."""
    from src.server.handlers.workflow_handler import cancel_workflow

    patches, registry_store, manager, _tracker, _update = _patch_common(
        manager_cancel_returns=True,
        has_active_returns=True,
        manual_compaction_returns=True,  # would early-return if reached
    )
    for p in patches:
        p.start()
    try:
        result = await cancel_workflow("t-1")
    finally:
        for p in patches:
            p.stop()

    assert result["cancelled"] is True
    # has_active short-circuits the `and` before cancel_compaction is evaluated.
    manager.cancel_compaction.assert_not_called()
    manager.cancel_workflow.assert_awaited_once_with("t-1", None)
