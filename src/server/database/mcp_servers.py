"""Database CRUD for per-workspace and user-level MCP server configuration.

Three concerns live here:
- User-level catalog (``user_mcp_servers``): templates the UI copies into a
  workspace on demand. Plain CRUD by ``(user_id, name)``.
- Per-workspace rows (``workspace_mcp_servers``): the source of truth for a
  workspace's effective MCP set. EVERY write bumps ``workspaces.mcp_config_version``
  in the SAME transaction so sessions can detect drift on their next acquire.
- Discovery schema cache (``workspace_mcp_tool_schemas``): tool snapshots keyed
  by ``(workspace_id, server_name, config_version)``.

Secrets are never stored here — env/header values hold ``${vault:NAME}``
references resolved against ``workspace_vault_secrets`` inside the sandbox.
"""

import logging
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.conversation import get_db_connection

logger = logging.getLogger(__name__)

# Hard cap on user-configured (source='workspace') servers per workspace.
MAX_MCP_SERVERS_PER_WORKSPACE = 20


# ---------------------------------------------------------------------------
# User-level catalog (templates)
# ---------------------------------------------------------------------------


async def list_catalog_servers(user_id: str) -> list[dict[str, Any]]:
    """List all catalog templates for a user, ordered by name."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_mcp_server_id, user_id, name, transport, command, args,
                       url, env, headers, description, instruction, tool_exposure_mode,
                       created_at, updated_at
                FROM user_mcp_servers
                WHERE user_id = %s
                ORDER BY name
                """,
                (user_id,),
            )
            return [_catalog_row_to_dict(r) for r in await cur.fetchall()]


async def get_catalog_server(user_id: str, name: str) -> dict[str, Any] | None:
    """Return a single catalog template by name, or None."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_mcp_server_id, user_id, name, transport, command, args,
                       url, env, headers, description, instruction, tool_exposure_mode,
                       created_at, updated_at
                FROM user_mcp_servers
                WHERE user_id = %s AND name = %s
                """,
                (user_id, name),
            )
            row = await cur.fetchone()
            return _catalog_row_to_dict(row) if row else None


async def create_catalog_server(
    user_id: str,
    name: str,
    *,
    transport: str = "stdio",
    command: str | None = None,
    args: list[str] | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    description: str = "",
    instruction: str = "",
    tool_exposure_mode: str = "summary",
) -> dict[str, Any]:
    """Insert a catalog template. Raises ValueError on duplicate name."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO user_mcp_servers
                    (user_id, name, transport, command, args, url, env, headers,
                     description, instruction, tool_exposure_mode, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (user_id, name) DO NOTHING
                RETURNING user_mcp_server_id, user_id, name, transport, command, args,
                          url, env, headers, description, instruction, tool_exposure_mode,
                          created_at, updated_at
                """,
                (
                    user_id, name, transport, command, Json(args or []), url,
                    Json(env or {}), Json(headers or {}), description, instruction,
                    tool_exposure_mode,
                ),
            )
            row = await cur.fetchone()
            if not row:
                raise ValueError(
                    f"MCP catalog server {name!r} already exists for this user"
                )
            logger.info(f"[mcp_db] create_catalog_server user_id={user_id} name={name}")
            return _catalog_row_to_dict(row)


async def update_catalog_server(
    user_id: str, name: str, *, updates: dict[str, Any]
) -> dict[str, Any] | None:
    """Partial update of a catalog template. Returns the row, or None if absent."""
    if not updates:
        return await get_catalog_server(user_id, name)

    # Whitelist mutable columns; JSONB columns are wrapped in Json().
    _jsonb_cols = {"args", "env", "headers"}
    _scalar_cols = {
        "transport", "command", "url", "description", "instruction",
        "tool_exposure_mode",
    }
    parts: list[str] = []
    params: list[Any] = []
    for col, val in updates.items():
        if col in _jsonb_cols:
            parts.append(f"{col} = %s")
            params.append(Json(val))
        elif col in _scalar_cols:
            parts.append(f"{col} = %s")
            params.append(val)
    if not parts:
        return await get_catalog_server(user_id, name)
    parts.append("updated_at = NOW()")
    params.extend([user_id, name])

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE user_mcp_servers SET {', '.join(parts)} "
                "WHERE user_id = %s AND name = %s "
                "RETURNING user_mcp_server_id, user_id, name, transport, command, args, "
                "url, env, headers, description, instruction, tool_exposure_mode, "
                "created_at, updated_at",
                params,
            )
            row = await cur.fetchone()
            if not row:
                return None
            logger.info(f"[mcp_db] update_catalog_server user_id={user_id} name={name}")
            return _catalog_row_to_dict(row)


async def delete_catalog_server(user_id: str, name: str) -> bool:
    """Delete a catalog template by name. Returns True if a row existed."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM user_mcp_servers WHERE user_id = %s AND name = %s",
                (user_id, name),
            )
            if cur.rowcount == 0:
                return False
            logger.info(f"[mcp_db] delete_catalog_server user_id={user_id} name={name}")
            return True


# ---------------------------------------------------------------------------
# Per-workspace rows (source of truth) — every write bumps mcp_config_version
# ---------------------------------------------------------------------------


async def list_workspace_servers(workspace_id: str) -> list[dict[str, Any]]:
    """List all MCP rows for a workspace (both disable-markers and user servers)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_mcp_server_id, workspace_id, name, source, enabled,
                       config, created_at, updated_at
                FROM workspace_mcp_servers
                WHERE workspace_id = %s
                ORDER BY name
                """,
                (workspace_id,),
            )
            return [_workspace_row_to_dict(r) for r in await cur.fetchall()]


async def upsert_workspace_server(
    workspace_id: str,
    name: str,
    *,
    source: str,
    enabled: bool,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert or update a workspace MCP row; bumps mcp_config_version in the txn.

    On insert of a new ``source='workspace'`` row, enforces
    ``MAX_MCP_SERVERS_PER_WORKSPACE`` under an advisory lock so concurrent
    creates can't slip past the cap. Disable-markers (``source='builtin'``)
    do not count against the cap.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Serialize concurrent mutations for this workspace.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s::text))",
                    (workspace_id,),
                )
                if source == "workspace":
                    await cur.execute(
                        """
                        SELECT COUNT(*) AS cnt FROM workspace_mcp_servers
                        WHERE workspace_id = %s AND source = 'workspace'
                          AND name <> %s
                        """,
                        (workspace_id, name),
                    )
                    cnt = (await cur.fetchone())["cnt"]
                    if cnt >= MAX_MCP_SERVERS_PER_WORKSPACE:
                        raise ValueError(
                            f"Maximum of {MAX_MCP_SERVERS_PER_WORKSPACE} "
                            "MCP servers per workspace reached"
                        )

                await cur.execute(
                    """
                    INSERT INTO workspace_mcp_servers
                        (workspace_id, name, source, enabled, config, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (workspace_id, name) DO UPDATE
                        SET source = EXCLUDED.source,
                            enabled = EXCLUDED.enabled,
                            config = EXCLUDED.config,
                            updated_at = NOW()
                    RETURNING workspace_mcp_server_id, workspace_id, name, source,
                              enabled, config, created_at, updated_at
                    """,
                    (
                        workspace_id, name, source, enabled,
                        Json(config) if config is not None else None,
                    ),
                )
                row = await cur.fetchone()
                await _bump_version(cur, workspace_id)
                logger.info(
                    f"[mcp_db] upsert_workspace_server workspace_id={workspace_id} "
                    f"name={name} source={source} enabled={enabled}"
                )
                return _workspace_row_to_dict(row)


async def set_workspace_server_enabled(
    workspace_id: str, name: str, enabled: bool
) -> bool:
    """Toggle a workspace MCP row's enabled flag; bumps version. False if absent."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s::text))",
                    (workspace_id,),
                )
                await cur.execute(
                    "UPDATE workspace_mcp_servers SET enabled = %s, updated_at = NOW() "
                    "WHERE workspace_id = %s AND name = %s",
                    (enabled, workspace_id, name),
                )
                if cur.rowcount == 0:
                    return False
                await _bump_version(cur, workspace_id)
                logger.info(
                    f"[mcp_db] set_workspace_server_enabled workspace_id={workspace_id} "
                    f"name={name} enabled={enabled}"
                )
                return True


async def delete_workspace_server(workspace_id: str, name: str) -> bool:
    """Delete a workspace MCP row; bumps version. False if no row existed."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s::text))",
                    (workspace_id,),
                )
                await cur.execute(
                    "DELETE FROM workspace_mcp_servers "
                    "WHERE workspace_id = %s AND name = %s",
                    (workspace_id, name),
                )
                if cur.rowcount == 0:
                    return False
                await _bump_version(cur, workspace_id)
                logger.info(
                    f"[mcp_db] delete_workspace_server workspace_id={workspace_id} "
                    f"name={name}"
                )
                return True


# ---------------------------------------------------------------------------
# Discovery schema cache
# ---------------------------------------------------------------------------


async def get_tool_schemas(
    workspace_id: str, config_version: int
) -> list[dict[str, Any]]:
    """Return cached tool-schema rows for a workspace at a given config version."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id, server_name, config_version, tools, status,
                       error, observed_meta, discovered_at
                FROM workspace_mcp_tool_schemas
                WHERE workspace_id = %s AND config_version = %s
                ORDER BY server_name
                """,
                (workspace_id, config_version),
            )
            return [_schema_row_to_dict(r) for r in await cur.fetchall()]


async def upsert_tool_schemas(
    workspace_id: str,
    server_name: str,
    config_version: int,
    *,
    tools: list[dict[str, Any]] | None = None,
    status: str = "pending",
    error: str = "",
    observed_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert or replace a discovery snapshot for one server at one version."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO workspace_mcp_tool_schemas
                    (workspace_id, server_name, config_version, tools, status,
                     error, observed_meta, discovered_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (workspace_id, server_name, config_version) DO UPDATE
                    SET tools = EXCLUDED.tools,
                        status = EXCLUDED.status,
                        error = EXCLUDED.error,
                        observed_meta = EXCLUDED.observed_meta,
                        discovered_at = NOW()
                RETURNING workspace_id, server_name, config_version, tools, status,
                          error, observed_meta, discovered_at
                """,
                (
                    workspace_id, server_name, config_version, Json(tools or []),
                    status, error, Json(observed_meta or {}),
                ),
            )
            return _schema_row_to_dict(await cur.fetchone())


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _bump_version(cur, workspace_id: str) -> None:
    """Atomically increment a workspace's mcp_config_version (same txn)."""
    await cur.execute(
        "UPDATE workspaces SET mcp_config_version = mcp_config_version + 1 "
        "WHERE workspace_id = %s",
        (workspace_id,),
    )


def _catalog_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a user_mcp_servers row into a plain JSON-friendly dict."""
    return {
        "user_mcp_server_id": str(row["user_mcp_server_id"]),
        "user_id": row["user_id"],
        "name": row["name"],
        "transport": row["transport"],
        "command": row["command"],
        "args": row["args"] or [],
        "url": row["url"],
        "env": row["env"] or {},
        "headers": row["headers"] or {},
        "description": row["description"] or "",
        "instruction": row["instruction"] or "",
        "tool_exposure_mode": row["tool_exposure_mode"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _workspace_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a workspace_mcp_servers row into a plain dict."""
    return {
        "workspace_mcp_server_id": str(row["workspace_mcp_server_id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "source": row["source"],
        "enabled": row["enabled"],
        "config": row["config"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _schema_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a workspace_mcp_tool_schemas row into a plain dict."""
    return {
        "workspace_id": str(row["workspace_id"]),
        "server_name": row["server_name"],
        "config_version": row["config_version"],
        "tools": row["tools"] or [],
        "status": row["status"],
        "error": row["error"] or "",
        "observed_meta": row["observed_meta"] or {},
        "discovered_at": row["discovered_at"].isoformat(),
    }
