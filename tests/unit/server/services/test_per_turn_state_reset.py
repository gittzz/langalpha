"""Tests for per-turn state reset across workflow executions.

Two related fixes covered here:

1. ``BackgroundTaskManager._reset_event_buffer_state`` drops
   ``workflow:stream:{thread_id}`` + ``workflow:events:meta:{thread_id}``
   at the start of every fresh turn so a new SSE consumer can't replay
   stale events from a previous turn.

2. ``ConversationPersistenceService._finalize_pair`` now releases the
   thread's singleton from the module cache (matching the documented
   "one instance per workflow execution" lifecycle), and
   ``persist_query_start`` no longer gates on the in-memory
   ``_persisted_queries`` set — the DB's
   ``ON CONFLICT (conversation_thread_id, turn_index)`` is the source
   of truth for idempotency.

Together these stop a turn-2 query from rendering the turn-1 response
even when the previous turn's terminal cleanup didn't run cleanly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.background_task_manager import BackgroundTaskManager
from src.server.services.persistence import conversation as persistence_module
from src.server.services.persistence.conversation import (
    ConversationPersistenceService,
)


# ---------------------------------------------------------------------------
# Fix #1 — BTM proactive event-buffer reset
# ---------------------------------------------------------------------------


def _make_btm_redis_backend() -> BackgroundTaskManager:
    """BackgroundTaskManager configured with the redis event backend."""
    with patch("src.server.services.background_task_manager.get_max_concurrent_workflows", return_value=10), \
         patch("src.server.services.background_task_manager.get_workflow_result_ttl", return_value=3600), \
         patch("src.server.services.background_task_manager.get_abandoned_workflow_timeout", return_value=3600), \
         patch("src.server.services.background_task_manager.get_cleanup_interval", return_value=60), \
         patch("src.server.services.background_task_manager.is_intermediate_storage_enabled", return_value=False), \
         patch("src.server.services.background_task_manager.get_max_stored_messages_per_agent", return_value=1000), \
         patch("src.server.services.background_task_manager.get_event_storage_backend", return_value="redis"), \
         patch("src.server.services.background_task_manager.get_redis_ttl_workflow_events", return_value=86400):
        return BackgroundTaskManager()


def _patch_cache(client_mock: MagicMock):
    """Patch ``get_cache_client`` to return an enabled cache with the given client."""
    cache = MagicMock()
    cache.enabled = True
    cache.client = client_mock
    return patch(
        "src.server.services.background_task_manager.get_cache_client",
        return_value=cache,
    )


@pytest.mark.asyncio
async def test_reset_event_buffer_deletes_stream_and_meta():
    """``_reset_event_buffer_state`` pipelines DEL on stream + meta keys."""
    btm = _make_btm_redis_backend()

    pipe = MagicMock()
    pipe.delete = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[1, 1])

    client = MagicMock()
    client.pipeline = MagicMock(return_value=pipe)

    with _patch_cache(client):
        await btm._reset_event_buffer_state("thread-X")

    # Both keys queued for DEL on the pipeline before the single execute().
    deleted_keys = [call.args[0] for call in pipe.delete.call_args_list]
    assert "workflow:stream:thread-X" in deleted_keys
    assert "workflow:events:meta:thread-X" in deleted_keys
    pipe.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_event_buffer_noop_for_non_redis_backend():
    """Memory backend never touches Redis."""
    with patch("src.server.services.background_task_manager.get_max_concurrent_workflows", return_value=10), \
         patch("src.server.services.background_task_manager.get_workflow_result_ttl", return_value=3600), \
         patch("src.server.services.background_task_manager.get_abandoned_workflow_timeout", return_value=3600), \
         patch("src.server.services.background_task_manager.get_cleanup_interval", return_value=60), \
         patch("src.server.services.background_task_manager.is_intermediate_storage_enabled", return_value=False), \
         patch("src.server.services.background_task_manager.get_max_stored_messages_per_agent", return_value=1000), \
         patch("src.server.services.background_task_manager.get_event_storage_backend", return_value="memory"), \
         patch("src.server.services.background_task_manager.get_redis_ttl_workflow_events", return_value=86400):
        btm = BackgroundTaskManager()

    with patch(
        "src.server.services.background_task_manager.get_cache_client",
        side_effect=AssertionError("should not be called for memory backend"),
    ):
        await btm._reset_event_buffer_state("thread-X")


@pytest.mark.asyncio
async def test_pre_register_calls_reset_event_buffer():
    """``pre_register`` resets the buffer before any consumer can attach to the placeholder."""
    btm = _make_btm_redis_backend()

    with patch.object(btm, "_reset_event_buffer_state", new_callable=AsyncMock) as mock_reset:
        await btm.pre_register("thread-Y")

    mock_reset.assert_awaited_once_with("thread-Y")
    assert "thread-Y" in btm.tasks


# ---------------------------------------------------------------------------
# Fix #2 — Persistence singleton lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_persistence_singleton_cache():
    """Each test gets a clean module-level singleton cache."""
    persistence_module._service_instances.clear()
    yield
    persistence_module._service_instances.clear()


@pytest.mark.asyncio
async def test_finalize_pair_releases_singleton_from_cache():
    """After ``_finalize_pair`` runs, the next ``get_instance`` returns a fresh object."""
    svc = ConversationPersistenceService.get_instance("thread-Z")
    assert persistence_module._service_instances.get("thread-Z") is svc

    # ``_finalize_pair`` increments turn_index, fires the hook, then cleanups.
    svc._turn_index_cache = 3
    await svc._finalize_pair()

    # Singleton dropped from cache.
    assert "thread-Z" not in persistence_module._service_instances
    # The next get_instance builds a fresh object — separate identity.
    fresh = ConversationPersistenceService.get_instance("thread-Z")
    assert fresh is not svc


@pytest.mark.asyncio
async def test_persist_query_start_does_not_gate_on_stale_set():
    """Even when the in-memory set already contains the turn_index (simulating
    stale state from a prior workflow execution that didn't reach
    ``_finalize_pair``), persist_query_start must still call create_query —
    the DB's ON CONFLICT is the canonical idempotency check."""
    svc = ConversationPersistenceService.get_instance("thread-Q")
    # Simulate stale carry-over: previous turn left turn_index=1 in the set.
    svc._persisted_queries.add(1)
    svc._turn_index_cache = 1

    db_returned_uuid = "11111111-1111-1111-1111-111111111111"
    create_query_mock = AsyncMock(
        return_value={"conversation_query_id": db_returned_uuid, "turn_index": 1}
    )

    with patch(
        "src.server.services.persistence.conversation.qr_db.create_query",
        create_query_mock,
    ):
        returned = await svc.persist_query_start(
            content="hello",
            query_type="initial",
        )

    create_query_mock.assert_awaited_once()
    kwargs = create_query_mock.call_args.kwargs
    assert kwargs["conversation_thread_id"] == "thread-Q"
    assert kwargs["turn_index"] == 1
    assert returned == db_returned_uuid
    assert svc._current_query_id == db_returned_uuid


@pytest.mark.asyncio
async def test_persist_query_start_uses_freshly_generated_id_when_row_lacks_one():
    """If the DB layer returns a dict without ``conversation_query_id`` (defensive
    path), fall back to the freshly generated UUID we sent."""
    svc = ConversationPersistenceService.get_instance("thread-R")
    svc._turn_index_cache = 0

    create_query_mock = AsyncMock(return_value={"turn_index": 0})

    with patch(
        "src.server.services.persistence.conversation.qr_db.create_query",
        create_query_mock,
    ):
        returned = await svc.persist_query_start(content="hi", query_type="initial")

    sent_id = create_query_mock.call_args.kwargs["conversation_query_id"]
    assert returned == sent_id
    assert svc._current_query_id == sent_id
