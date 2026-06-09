"""Integration tests for MCP server CRUD against real PostgreSQL.

Covers the user-level catalog, per-workspace rows (each write bumping
``mcp_config_version`` in the same txn), the 20-server cap, and the
version-keyed discovery schema cache.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _version(workspace_id: str) -> int:
    from src.server.database.workspace import get_workspace

    ws = await get_workspace(workspace_id)
    return int(ws["mcp_config_version"])


# ---------------------------------------------------------------------------
# Catalog CRUD
# ---------------------------------------------------------------------------


class TestCatalogCrud:
    async def test_create_and_get(self, seed_user, patched_get_db_connection):
        from src.server.database.mcp_servers import (
            create_catalog_server,
            get_catalog_server,
        )

        await create_catalog_server(
            seed_user["user_id"], "acme",
            transport="http", url="https://example.test/mcp",
            headers={"Authorization": "${vault:TOKEN}"},
            description="d", instruction="i", tool_exposure_mode="detailed",
        )
        row = await get_catalog_server(seed_user["user_id"], "acme")
        assert row["name"] == "acme"
        assert row["headers"] == {"Authorization": "${vault:TOKEN}"}
        assert row["tool_exposure_mode"] == "detailed"

    async def test_duplicate_name_raises(self, seed_user, patched_get_db_connection):
        from src.server.database.mcp_servers import create_catalog_server

        await create_catalog_server(seed_user["user_id"], "dup", command="npx")
        with pytest.raises(ValueError):
            await create_catalog_server(seed_user["user_id"], "dup", command="npx")

    async def test_update_and_list(self, seed_user, patched_get_db_connection):
        from src.server.database.mcp_servers import (
            create_catalog_server,
            list_catalog_servers,
            update_catalog_server,
        )

        await create_catalog_server(seed_user["user_id"], "acme", command="npx")
        updated = await update_catalog_server(
            seed_user["user_id"], "acme",
            updates={"description": "new", "args": ["-y", "pkg"]},
        )
        assert updated["description"] == "new"
        assert updated["args"] == ["-y", "pkg"]
        rows = await list_catalog_servers(seed_user["user_id"])
        assert [r["name"] for r in rows] == ["acme"]

    async def test_delete(self, seed_user, patched_get_db_connection):
        from src.server.database.mcp_servers import (
            create_catalog_server,
            delete_catalog_server,
            get_catalog_server,
        )

        await create_catalog_server(seed_user["user_id"], "acme", command="npx")
        assert await delete_catalog_server(seed_user["user_id"], "acme") is True
        assert await delete_catalog_server(seed_user["user_id"], "acme") is False
        assert await get_catalog_server(seed_user["user_id"], "acme") is None


# ---------------------------------------------------------------------------
# Workspace rows — version bump in the same txn
# ---------------------------------------------------------------------------


class TestWorkspaceRows:
    async def test_upsert_bumps_version(self, seed_workspace, patched_get_db_connection):
        from src.server.database.mcp_servers import upsert_workspace_server

        wid = seed_workspace["workspace_id"]
        assert await _version(wid) == 0

        await upsert_workspace_server(
            wid, "acme", source="workspace", enabled=True,
            config={"transport": "stdio", "command": "npx"},
        )
        assert await _version(wid) == 1

        # Update (same name) bumps again.
        await upsert_workspace_server(
            wid, "acme", source="workspace", enabled=True,
            config={"transport": "stdio", "command": "uvx"},
        )
        assert await _version(wid) == 2

    async def test_disable_marker_bumps_version(self, seed_workspace, patched_get_db_connection):
        from src.server.database.mcp_servers import (
            list_workspace_servers,
            upsert_workspace_server,
        )

        wid = seed_workspace["workspace_id"]
        await upsert_workspace_server(
            wid, "builtin-x", source="builtin", enabled=False, config=None,
        )
        assert await _version(wid) == 1
        rows = await list_workspace_servers(wid)
        assert rows[0]["source"] == "builtin"
        assert rows[0]["enabled"] is False
        assert rows[0]["config"] is None

    async def test_set_enabled_and_delete_bump(self, seed_workspace, patched_get_db_connection):
        from src.server.database.mcp_servers import (
            delete_workspace_server,
            set_workspace_server_enabled,
            upsert_workspace_server,
        )

        wid = seed_workspace["workspace_id"]
        await upsert_workspace_server(
            wid, "acme", source="workspace", enabled=True,
            config={"transport": "stdio"},
        )  # v1
        assert await set_workspace_server_enabled(wid, "acme", False) is True  # v2
        assert await _version(wid) == 2
        assert await delete_workspace_server(wid, "acme") is True  # v3
        assert await _version(wid) == 3
        # Absent rows don't bump.
        assert await set_workspace_server_enabled(wid, "nope", True) is False
        assert await delete_workspace_server(wid, "nope") is False
        assert await _version(wid) == 3

    async def test_cap_enforced(self, seed_workspace, patched_get_db_connection):
        from src.server.database.mcp_servers import (
            MAX_MCP_SERVERS_PER_WORKSPACE,
            upsert_workspace_server,
        )

        wid = seed_workspace["workspace_id"]
        for i in range(MAX_MCP_SERVERS_PER_WORKSPACE):
            await upsert_workspace_server(
                wid, f"srv-{i}", source="workspace", enabled=True,
                config={"transport": "stdio"},
            )
        with pytest.raises(ValueError):
            await upsert_workspace_server(
                wid, "one-too-many", source="workspace", enabled=True,
                config={"transport": "stdio"},
            )
        # Updating an existing server at the cap still works (not a new insert).
        await upsert_workspace_server(
            wid, "srv-0", source="workspace", enabled=False,
            config={"transport": "stdio"},
        )


# ---------------------------------------------------------------------------
# Discovery schema cache
# ---------------------------------------------------------------------------


class TestSchemaCache:
    async def test_upsert_and_get_keyed_by_version(self, seed_workspace, patched_get_db_connection):
        from src.server.database.mcp_servers import (
            get_tool_schemas,
            upsert_tool_schemas,
        )

        wid = seed_workspace["workspace_id"]
        await upsert_tool_schemas(
            wid, "acme", 1,
            tools=[{"name": "t1", "description": "d", "input_schema": {}}],
            status="ok",
        )
        # Different version is a distinct cache entry.
        await upsert_tool_schemas(wid, "acme", 2, status="pending")

        v1 = await get_tool_schemas(wid, 1)
        assert len(v1) == 1 and v1[0]["status"] == "ok"
        assert v1[0]["tools"][0]["name"] == "t1"

        v2 = await get_tool_schemas(wid, 2)
        assert len(v2) == 1 and v2[0]["status"] == "pending"

    async def test_upsert_replaces_same_key(self, seed_workspace, patched_get_db_connection):
        from src.server.database.mcp_servers import (
            get_tool_schemas,
            upsert_tool_schemas,
        )

        wid = seed_workspace["workspace_id"]
        await upsert_tool_schemas(wid, "acme", 1, status="pending")
        await upsert_tool_schemas(
            wid, "acme", 1, status="error", error="boom",
        )
        rows = await get_tool_schemas(wid, 1)
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert rows[0]["error"] == "boom"
