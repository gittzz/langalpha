"""Per-workspace MCP configuration resolution — the single chokepoint.

Modeled on ``resolve_llm_config``. Merges the process-global built-in MCP
servers (from ``base_config.mcp.servers``) with a workspace's DB-backed rows
into one deterministic effective set:

    effective = built-ins (config order)
                MINUS names disabled by a (source='builtin', enabled=false) row
                PLUS  source='workspace' enabled rows (alphabetical, appended)

The merged list and the DB↔model converter are defined ONCE here so the API
effective-list endpoint and the sandbox-sync path can import the same logic
(no prompt/wrapper divergence).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ptc_agent.config.core import MCPServerConfig

from ._common import logger

# Vault-reference resolution (``${vault:NAME}``) happens in-sandbox in Phase 2,
# not here — this module is the merge/convert chokepoint only. The canonical
# pattern lives in ``ptc_agent.core.mcp_sanitize.VAULT_REF_RE`` (Lane A); the
# Phase 2 secret-resolution codegen should import it from there.


@dataclass(frozen=True)
class ResolvedMCP:
    """The effective MCP server set for one workspace at one config version.

    ``servers`` is deterministic (built-ins in config order, then user servers
    alphabetical). ``builtin_names`` / ``user_names`` partition the effective
    set by origin. ``version`` is ``workspaces.mcp_config_version``.
    """

    servers: list[MCPServerConfig]
    builtin_names: frozenset[str]
    user_names: frozenset[str]
    version: int


def workspace_row_to_server_config(row: dict) -> MCPServerConfig:
    """Convert a ``workspace_mcp_servers`` row into an ``MCPServerConfig``.

    Defined ONCE; imported by the API and sandbox-sync lanes. ``source`` is
    forced to ``"workspace"`` and any stored ``vault_blueprints`` key is
    stripped (defense in depth — user servers never declare blueprints).
    """
    config = dict(row.get("config") or {})
    config.pop("vault_blueprints", None)
    config.pop("source", None)  # never trust a stored source tag
    # The row's name is authoritative over any name baked into the JSON blob.
    config["name"] = row["name"]
    config["source"] = "workspace"
    config["enabled"] = bool(row.get("enabled", True))
    return MCPServerConfig(**config)


async def resolve_mcp_config(
    base_config,
    user_id: str,
    workspace_id: str,
) -> ResolvedMCP:
    """Resolve the effective MCP server set for ``workspace_id``.

    Built-ins come from ``base_config.mcp.servers`` (enabled ones, config
    order); a ``(source='builtin', enabled=false)`` row removes a built-in by
    name; ``source='workspace'`` enabled rows are appended alphabetically. A
    workspace with zero rows returns the built-in objects unchanged (no copies)
    so zero-user-server workspaces stay byte-identical downstream.
    """
    from src.server.database.mcp_servers import list_workspace_servers
    from src.server.database.workspace import get_workspace

    # Built-ins from the global config, enabled only, in declaration order.
    builtin_servers = [
        s for s in base_config.mcp.servers
        if getattr(s, "enabled", True)
    ]
    builtin_name_set = {s.name for s in builtin_servers}

    # Two independent reads — the workspace rows and the version counter —
    # batched so the resolver pays one round-trip, not two.
    rows, version = await asyncio.gather(
        list_workspace_servers(workspace_id),
        _read_config_version(get_workspace, workspace_id),
    )

    # Short-circuit: no workspace rows ⇒ the effective set IS the built-in
    # list (same objects, no copies) so the hot path stays byte-identical.
    if not rows:
        return ResolvedMCP(
            servers=builtin_servers,
            builtin_names=frozenset(builtin_name_set),
            user_names=frozenset(),
            version=version,
        )

    disabled_builtins: set[str] = set()
    user_servers: list[MCPServerConfig] = []
    for row in rows:
        if row["source"] == "builtin":
            # Disable-marker: only acts when it turns a built-in off.
            if not row["enabled"]:
                disabled_builtins.add(row["name"])
            continue
        # source == 'workspace'
        if not row["enabled"]:
            continue
        if row["name"] in builtin_name_set:
            # Backstop for the API's 409: a user server must never collide with
            # a built-in name. Skip + log; do not let it shadow the built-in.
            logger.warning(
                "[MCP] Skipping workspace server %r in workspace %s: name "
                "collides with a built-in (API should reject at write).",
                row["name"], workspace_id,
            )
            continue
        try:
            user_servers.append(workspace_row_to_server_config(row))
        except Exception:
            logger.error(
                "[MCP] Failed to parse workspace server %r in workspace %s; "
                "skipping.", row["name"], workspace_id, exc_info=True,
            )

    effective_builtins = [
        s for s in builtin_servers if s.name not in disabled_builtins
    ]
    user_servers.sort(key=lambda s: s.name)

    return ResolvedMCP(
        servers=[*effective_builtins, *user_servers],
        builtin_names=frozenset(s.name for s in effective_builtins),
        user_names=frozenset(s.name for s in user_servers),
        version=version,
    )


async def _read_config_version(get_workspace, workspace_id: str) -> int:
    """Read ``mcp_config_version`` for a workspace, defaulting to 0."""
    ws = await get_workspace(workspace_id)
    if not ws:
        return 0
    return int(ws.get("mcp_config_version") or 0)
