"""Credential scrubbing at the conversation error persistence boundary."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.persistence import conversation as persistence_module
from src.server.services.persistence.conversation import (
    ConversationPersistenceService,
)


class _ConnectionContext:
    async def __aenter__(self):
        conn = MagicMock()
        conn.transaction = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=None),
                __aexit__=AsyncMock(return_value=None),
            )
        )
        return conn

    async def __aexit__(self, *args):
        return None


async def _persisted_errors(error_message: str, errors: list | None = None) -> list:
    service = ConversationPersistenceService(
        "thread-error", "11111111-2222-3333-4444-555555555555"
    )
    create_response = AsyncMock(return_value=None)

    with (
        patch.object(
            persistence_module.qr_db,
            "get_next_turn_index",
            AsyncMock(return_value=0),
        ),
        patch.object(
            persistence_module.qr_db, "create_response", create_response
        ),
        patch.object(
            persistence_module.qr_db,
            "update_thread_status",
            AsyncMock(return_value=None),
        ),
        patch.object(
            persistence_module.qr_db,
            "get_db_connection",
            return_value=_ConnectionContext(),
        ),
        patch.object(
            service, "_get_latest_checkpoint_id", AsyncMock(return_value=None)
        ),
        patch.object(service, "_finalize_pair", AsyncMock(return_value=None)),
    ):
        await service.persist_error(error_message=error_message, errors=errors)

    return create_response.await_args.kwargs["errors"]


@pytest.mark.asyncio
async def test_default_error_message_is_sanitized_before_persistence():
    persisted = await _persisted_errors(
        "GET https://user:hunter2@api.example.test/v1 failed; "
        "api_key=sk-abcdef0123456789"
    )

    assert persisted == [
        "GET https://api.example.test/v1 failed; api_key=[REDACTED]"
    ]


@pytest.mark.asyncio
async def test_explicit_error_list_is_sanitized_before_persistence():
    persisted = await _persisted_errors(
        "unused",
        errors=[
            "Authorization: Bearer abc123def456xyz",
            "safe diagnostic",
        ],
    )

    assert persisted == [
        "Authorization: Bearer [REDACTED]",
        "safe diagnostic",
    ]
