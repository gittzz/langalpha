"""IDOR guard in ``_handle_send_message`` (shared by both POST message routes).

An existing thread must belong to the caller. The guard reads the thread's
owner via ``get_thread_owner_id`` (thread -> workspace JOIN):

  - owner is another user  -> 403 Forbidden.
  - owner is the caller    -> proceeds.
  - no owner (brand-new thread_id, owner is None) -> proceeds (creation).
  - internal report-back dispatch sets ``X-User-Id`` to the thread owner, so
    ``auth.user_id == owner_id`` and the guard passes.

The guard fires before workspace resolution, so the passing cases only need the
downstream (resolve/credit/stream) chain stubbed to reach a 200 stream.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ptc_agent.config.agent import CredentialSource
from tests.conftest import create_test_app

CALLER = "usr-caller-001"
OWNER = "usr-owner-002"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config():
    """Stub ``AgentConfig`` returned by ``resolve_llm_config`` (platform key)."""
    cfg = MagicMock()
    cfg.credential_source = CredentialSource.PLATFORM
    cfg.llm_client = None
    cfg.llm = MagicMock()
    cfg.llm.name = "claude-sonnet-placeholder"
    return cfg


def _empty_async_gen():
    async def _gen():
        if False:
            yield ""

    return _gen()


def _app_for(user_id: str):
    """Threads router with the chat auth dependency pinned to ``user_id``.

    ``access_tier=0`` keeps the no-provider 403 guard from firing first, so the
    IDOR guard is the only thing under test.
    """
    from src.server.app.threads import router
    from src.server.dependencies.usage_limits import ChatAuthResult, enforce_chat_limit

    app = create_test_app(router)
    app.dependency_overrides[enforce_chat_limit] = lambda: ChatAuthResult(
        user_id=user_id, access_tier=0
    )
    return app


@contextmanager
def _stub_downstream(owner_id):
    """Patch the IDOR guard's owner lookup + everything past it.

    With ``owner_id`` matching the caller (or ``None``), a POST should sail
    through the guard and reach a 200 SSE stream.
    """
    from src.server.app import setup as setup_module

    wm_singleton = MagicMock()
    wm_singleton.has_ready_session.return_value = True

    with (
        patch(
            "src.server.app.threads.get_thread_owner_id",
            new=AsyncMock(return_value=owner_id),
        ),
        patch.object(setup_module, "agent_config", MagicMock()),
        patch(
            "src.server.handlers.chat.resolve_llm_config",
            new=AsyncMock(return_value=_make_config()),
        ),
        patch(
            "src.server.dependencies.usage_limits.enforce_credit_limit",
            new=AsyncMock(),
        ),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            return_value=wm_singleton,
        ),
        patch(
            "src.server.handlers.chat.astream_ptc_workflow",
            return_value=_empty_async_gen(),
        ),
        patch(
            "src.server.app.threads.observe_chat_stream",
            side_effect=lambda gen, **_: gen,
        ),
    ):
        yield


_PTC_BODY = {
    "workspace_id": "ws-placeholder",
    "messages": [{"role": "user", "content": "test query"}],
    "agent_mode": "ptc",
}


async def _post(app, path: str, *, headers=None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.post(path, json=_PTC_BODY, headers=headers or {})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_existing_thread_owned_by_other_user_is_forbidden():
    """The core IDOR: caller POSTs to a thread owned by someone else -> 403."""
    app = _app_for(CALLER)
    with (
        patch(
            "src.server.app.threads.get_thread_owner_id",
            new=AsyncMock(return_value=OWNER),
        ),
        # The 403 unwinds through the burst-slot release; keep it hermetic.
        patch(
            "src.server.dependencies.usage_limits.release_burst_slot",
            new=AsyncMock(),
        ),
    ):
        resp = await _post(app, "/api/v1/threads/tid-existing/messages")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Forbidden"


@pytest.mark.asyncio
async def test_post_existing_thread_owned_by_caller_proceeds():
    """Caller owns the thread (owner_id == user_id) -> guard passes, 200."""
    app = _app_for(CALLER)
    with _stub_downstream(owner_id=CALLER):
        resp = await _post(app, "/api/v1/threads/tid-mine/messages")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_new_thread_no_owner_proceeds():
    """Brand-new thread (get_thread_owner_id -> None) -> guard passes, 200.

    Uses the new-thread route, which mints a fresh uuid before the guard runs.
    """
    app = _app_for(CALLER)
    with _stub_downstream(owner_id=None):
        resp = await _post(app, "/api/v1/threads/messages")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_internal_dispatch_with_owner_user_id_proceeds():
    """Report-back dispatch sets X-User-Id to the owner -> owner_id == user_id.

    The auth dependency reflects that X-User-Id resolution by pinning the auth
    user to the owner; the guard must let the dispatch through (no 403).
    """
    app = _app_for(OWNER)
    with _stub_downstream(owner_id=OWNER):
        resp = await _post(
            app,
            "/api/v1/threads/tid-dispatch/messages",
            headers={"X-Dispatch": "background"},
        )
    assert resp.status_code == 200
