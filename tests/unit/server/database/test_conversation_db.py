"""
Tests for src/server/database/conversation.py

Verifies thread CRUD, query/response persistence, thread title updates,
thread sharing, and pagination logic.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# The conversation module defines get_db_connection itself, so the root
# conftest mock_db_connection fixture (which patches exactly that path)
# is sufficient here.
# ---------------------------------------------------------------------------


def _thread_row(
    thread_id=None,
    workspace_id=None,
    status="in_progress",
    title="Test Thread",
    **overrides,
):
    now = datetime.now(timezone.utc)
    row = {
        "conversation_thread_id": thread_id or str(uuid.uuid4()),
        "workspace_id": workspace_id or str(uuid.uuid4()),
        "current_status": status,
        "msg_type": "ptc",
        "thread_index": 0,
        "title": title,
        "is_shared": False,
        "share_token": None,
        "share_permissions": None,
        "shared_at": None,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


def _query_row(query_id=None, thread_id="t-1", turn_index=0, **overrides):
    now = datetime.now(timezone.utc)
    row = {
        "conversation_query_id": query_id or str(uuid.uuid4()),
        "conversation_thread_id": thread_id,
        "turn_index": turn_index,
        "content": "Hello",
        "type": "ptc",
        "feedback_action": None,
        "metadata": {},
        "created_at": now,
    }
    row.update(overrides)
    return row


def _response_row(response_id=None, thread_id="t-1", turn_index=0, **overrides):
    now = datetime.now(timezone.utc)
    row = {
        "conversation_response_id": response_id or str(uuid.uuid4()),
        "conversation_thread_id": thread_id,
        "turn_index": turn_index,
        "status": "completed",
        "interrupt_reason": None,
        "metadata": {},
        "warnings": [],
        "errors": [],
        "execution_time": 1.5,
        "created_at": now,
        "sse_events": None,
    }
    row.update(overrides)
    return row


# ===========================================================================
# Thread Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_calculate_next_thread_index(mock_db_connection, mock_cursor):
    """calculate_next_thread_index returns MAX(thread_index) + 1."""
    from src.server.database.conversation import calculate_next_thread_index

    mock_cursor.fetchone.return_value = {"next_index": 3}

    idx = await calculate_next_thread_index("ws-1")

    assert idx == 3
    sql = mock_cursor.execute.call_args[0][0]
    assert "COALESCE(MAX(thread_index), -1)" in sql


@pytest.mark.asyncio
async def test_create_thread(mock_db_connection, mock_cursor):
    """create_thread inserts a row and returns the new thread dict."""
    from src.server.database.conversation import create_thread

    row = _thread_row(thread_id="t-1", workspace_id="ws-1")
    # First call: calculate_next_thread_index fetchone; second: create_thread fetchone
    mock_cursor.fetchone.side_effect = [{"next_index": 0}, row]

    result = await create_thread(
        conversation_thread_id="t-1",
        workspace_id="ws-1",
        current_status="in_progress",
        msg_type="ptc",
    )

    assert result["conversation_thread_id"] == "t-1"
    assert result["workspace_id"] == "ws-1"


@pytest.mark.asyncio
async def test_update_thread_status(mock_db_connection, mock_cursor):
    """update_thread_status updates status and returns True."""
    from src.server.database.conversation import update_thread_status

    result = await update_thread_status("t-1", "completed")

    assert result is True
    sql = mock_cursor.execute.call_args[0][0]
    assert "current_status = %s" in sql
    params = mock_cursor.execute.call_args[0][1]
    assert params[0] == "completed"
    assert params[1] == "t-1"


@pytest.mark.asyncio
async def test_update_thread_status_with_checkpoint(mock_db_connection, mock_cursor):
    """update_thread_status with checkpoint_id includes it in the SQL."""
    from src.server.database.conversation import update_thread_status

    result = await update_thread_status("t-1", "interrupted", checkpoint_id="cp-42")

    assert result is True
    sql = mock_cursor.execute.call_args[0][0]
    assert "latest_checkpoint_id" in sql
    params = mock_cursor.execute.call_args[0][1]
    assert params[1] == "cp-42"


@pytest.mark.asyncio
async def test_get_thread_by_id_found(mock_db_connection, mock_cursor):
    """get_thread_by_id returns dict when thread exists."""
    from src.server.database.conversation import get_thread_by_id

    row = _thread_row(thread_id="t-1")
    mock_cursor.fetchone.return_value = row

    result = await get_thread_by_id("t-1")

    assert result is not None
    assert result["conversation_thread_id"] == "t-1"


@pytest.mark.asyncio
async def test_get_thread_by_id_not_found(mock_db_connection, mock_cursor):
    """get_thread_by_id returns None when thread does not exist."""
    from src.server.database.conversation import get_thread_by_id

    mock_cursor.fetchone.return_value = None

    result = await get_thread_by_id("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_update_thread_title(mock_db_connection, mock_cursor):
    """update_thread_title updates title and returns updated thread."""
    from src.server.database.conversation import update_thread_title

    row = _thread_row(title="New Title")
    mock_cursor.fetchone.return_value = row

    result = await update_thread_title("t-1", "New Title")

    assert result is not None
    assert result["title"] == "New Title"
    params = mock_cursor.execute.call_args[0][1]
    assert params[0] == "New Title"
    assert params[1] == "t-1"


@pytest.mark.asyncio
async def test_update_thread_title_not_found(mock_db_connection, mock_cursor):
    """update_thread_title returns None when thread not found."""
    from src.server.database.conversation import update_thread_title

    mock_cursor.fetchone.return_value = None

    result = await update_thread_title("nonexistent", "Title")
    assert result is None


@pytest.mark.asyncio
async def test_delete_thread(mock_db_connection, mock_cursor):
    """delete_thread executes DELETE and returns True."""
    from src.server.database.conversation import delete_thread

    result = await delete_thread("t-1")

    assert result is True
    sql = mock_cursor.execute.call_args[0][0]
    assert "DELETE FROM conversation_threads" in sql


# ===========================================================================
# Query Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_create_query_idempotent(mock_db_connection, mock_cursor):
    """create_query with idempotent=True uses ON CONFLICT DO UPDATE."""
    from src.server.database.conversation import create_query

    row = _query_row(query_id="q-1")
    mock_cursor.fetchone.return_value = row

    result = await create_query(
        conversation_query_id="q-1",
        conversation_thread_id="t-1",
        turn_index=0,
        content="Hello",
        query_type="ptc",
    )

    assert result["conversation_query_id"] == "q-1"
    sql = mock_cursor.execute.call_args[0][0]
    assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_get_queries_for_thread(mock_db_connection, mock_cursor):
    """get_queries_for_thread returns queries list and total count."""
    from src.server.database.conversation import get_queries_for_thread

    q1 = _query_row(turn_index=0)
    q2 = _query_row(turn_index=1)
    mock_cursor.fetchone.return_value = {"total": 2}
    mock_cursor.fetchall.return_value = [q1, q2]

    queries, total = await get_queries_for_thread("t-1")

    assert total == 2
    assert len(queries) == 2


# ===========================================================================
# Response Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_create_response(mock_db_connection, mock_cursor):
    """create_response inserts response and returns dict."""
    from src.server.database.conversation import create_response

    row = _response_row(response_id="r-1")
    mock_cursor.fetchone.return_value = row

    result = await create_response(
        conversation_response_id="r-1",
        conversation_thread_id="t-1",
        turn_index=0,
        status="completed",
        execution_time=2.0,
    )

    assert result["conversation_response_id"] == "r-1"
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_get_responses_for_thread(mock_db_connection, mock_cursor):
    """get_responses_for_thread returns responses and total count."""
    from src.server.database.conversation import get_responses_for_thread

    r1 = _response_row(turn_index=0)
    mock_cursor.fetchone.return_value = {"total": 1}
    mock_cursor.fetchall.return_value = [r1]

    responses, total = await get_responses_for_thread("t-1")

    assert total == 1
    assert len(responses) == 1


# ===========================================================================
# Workspace Threads / Pagination
# ===========================================================================


@pytest.mark.asyncio
async def test_get_workspace_threads(mock_db_connection, mock_cursor):
    """get_workspace_threads returns paginated threads and count."""
    from src.server.database.conversation import get_workspace_threads

    t1 = _thread_row()
    mock_cursor.fetchone.return_value = {"total": 1}
    mock_cursor.fetchall.return_value = [t1]

    threads, total = await get_workspace_threads("ws-1", limit=10, offset=0)

    assert total == 1
    assert len(threads) == 1


@pytest.mark.asyncio
async def test_get_workspace_threads_invalid_sort(mock_db_connection, mock_cursor):
    """get_workspace_threads falls back to 'updated_at' for invalid sort_by."""
    from src.server.database.conversation import get_workspace_threads

    mock_cursor.fetchone.return_value = {"total": 0}
    mock_cursor.fetchall.return_value = []

    threads, total = await get_workspace_threads(
        "ws-1", sort_by="invalid_field", sort_order="invalid"
    )

    assert total == 0
    # The SELECT query (second execute call) should use updated_at DESC
    select_sql = mock_cursor.execute.call_args_list[1][0][0]
    assert "updated_at" in select_sql
    assert "DESC" in select_sql


# ===========================================================================
# Thread sharing
# ===========================================================================


@pytest.mark.asyncio
async def test_update_thread_sharing(mock_db_connection, mock_cursor):
    """update_thread_sharing updates is_shared and optional share_token."""
    from src.server.database.conversation import update_thread_sharing

    row = _thread_row(is_shared=True, share_token="tok-abc")
    row["share_permissions"] = None
    row["shared_at"] = None
    mock_cursor.fetchone.return_value = row

    result = await update_thread_sharing("t-1", is_shared=True, share_token="tok-abc")

    assert result is not None
    sql = mock_cursor.execute.call_args[0][0]
    assert "is_shared = %s" in sql
    assert "share_token = %s" in sql


@pytest.mark.asyncio
async def test_get_thread_by_share_token(mock_db_connection, mock_cursor):
    """get_thread_by_share_token returns thread when token matches."""
    from src.server.database.conversation import get_thread_by_share_token

    row = _thread_row(is_shared=True, share_token="tok-abc")
    row["workspace_name"] = "My WS"
    mock_cursor.fetchone.return_value = row

    result = await get_thread_by_share_token("tok-abc")

    assert result is not None
    sql = mock_cursor.execute.call_args[0][0]
    assert "share_token = %s" in sql
    assert "is_shared = TRUE" in sql


@pytest.mark.asyncio
async def test_get_thread_by_share_token_not_found(mock_db_connection, mock_cursor):
    """get_thread_by_share_token returns None when no match."""
    from src.server.database.conversation import get_thread_by_share_token

    mock_cursor.fetchone.return_value = None

    result = await get_thread_by_share_token("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_thread_checkpoint_id(mock_db_connection, mock_cursor):
    """get_thread_checkpoint_id returns the stored checkpoint_id."""
    from src.server.database.conversation import get_thread_checkpoint_id

    mock_cursor.fetchone.return_value = {"latest_checkpoint_id": "cp-99"}

    result = await get_thread_checkpoint_id("t-1")
    assert result == "cp-99"


@pytest.mark.asyncio
async def test_get_thread_checkpoint_id_none(mock_db_connection, mock_cursor):
    """get_thread_checkpoint_id returns None when thread not found."""
    from src.server.database.conversation import get_thread_checkpoint_id

    mock_cursor.fetchone.return_value = None

    result = await get_thread_checkpoint_id("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_thread_owner_id_invalid_uuid_short_circuits(
    mock_db_connection, mock_cursor
):
    """A non-UUID thread_id returns None without touching the DB.

    Regression: a directory name (e.g. ``results``) reaching ``WHERE
    conversation_thread_id = %s`` against the uuid column raised psycopg
    InvalidTextRepresentation (22P02), which require_thread_owner surfaced as a
    spurious 500. The guard must short-circuit to None (→ clean 404) before any
    query.
    """
    from src.server.database.conversation import get_thread_owner_id

    for bad_id in ("results", "my_notes.md", "", None):
        assert await get_thread_owner_id(bad_id) is None

    mock_cursor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_thread_owner_id_normalizes_noncanonical_uuid(
    mock_db_connection, mock_cursor
):
    """A Python-parseable but non-canonical thread_id is bound to the query in
    canonical form, never raw.

    Regression: ``uuid.UUID()`` accepts forms postgres' uuid type rejects (e.g.
    ``urn:uuid:...``); a validate-only guard let those reach postgres →
    InvalidTextRepresentation (22P02) → spurious 500. The guard must normalize
    and bind the canonical string.
    """
    from src.server.database.conversation import get_thread_owner_id

    canonical = "12345678-1234-5678-1234-567812345678"
    mock_cursor.fetchone.return_value = None
    for variant in (f"urn:uuid:{canonical}", "{%s}" % canonical, canonical.upper()):
        mock_cursor.execute.reset_mock()
        await get_thread_owner_id(variant)
        params = mock_cursor.execute.call_args[0][1]
        assert params == (canonical,), f"{variant!r} bound {params!r}, not canonical"
