"""Shared MCP discovery service: run in-sandbox discovery, sanitize, cache.

Single implementation used by both the on-demand API probe and the session
Phase-2 sync path, so sanitization and the schema cache never diverge.
Discovery executes untrusted code merely to list tools — it runs without
vault access (the generated client substitutes inert placeholders).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from ptc_agent.config.core import MCPServerConfig
from ptc_agent.core.mcp_sanitize import (
    VAULT_REF_RE,
    sanitize_tool_name,
    sanitize_tool_text,
)

from src.server.database import mcp_servers as mcp_db

logger = logging.getLogger(__name__)

# Discovery-boundary caps for hostile/buggy servers (plan §6). The prompt-side
# detailed-mode caps live in the formatter; these bound what we cache at all.
MAX_TOOLS_PER_SERVER = 64
MAX_SCHEMA_CHARS_PER_SERVER = 200_000


def mcp_discovery_fingerprint(server: MCPServerConfig) -> str:
    """Stable per-server hash of discovery-affecting config — never secret values.

    Captures everything that can change a server's ``tools/list`` result:
    transport, command, args, url, env/header key NAMES + the ``${vault:NAME}``
    refs each key targets, and the secret-less-discovery decision. It deliberately
    EXCLUDES ``enabled`` (toggling a server off/on reuses its cached schema —
    nothing about its tools changed) and the prompt-only fields (description /
    instruction / tool_exposure_mode).

    This is the discovery-cache key, keyed off the server's OWN identity, so
    mutating or toggling an UNRELATED server never orphans this one's snapshot.
    Names only — literal secret values never enter the hash.
    """

    def _refs_by_key(mapping: dict | None) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key, value in (mapping or {}).items():
            refs = sorted(set(VAULT_REF_RE.findall(str(value))))
            if refs:
                out[key] = refs
        return out

    env = getattr(server, "env", {}) or {}
    headers = getattr(server, "headers", {}) or {}
    payload = {
        "transport": getattr(server, "transport", None),
        "command": getattr(server, "command", None),
        "args": list(getattr(server, "args", []) or []),
        "url": getattr(server, "url", None),
        "discovery_uses_secrets": bool(getattr(server, "discovery_uses_secrets", False)),
        "env_keys": sorted(env.keys()),
        "header_keys": sorted(headers.keys()),
        "env_vault_refs": _refs_by_key(env),
        "header_vault_refs": _refs_by_key(headers),
        "url_vault_refs": sorted(set(VAULT_REF_RE.findall(getattr(server, "url", "") or ""))),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def sanitize_discovered_tools(
    tools: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Sanitize one server's raw ``tools/list`` snapshot for caching.

    Keeps the ORIGINAL tool name (wrappers must call the server by its real
    name; identifier sanitization happens again at codegen), but drops tools
    whose names cannot become a legal identifier or that collide after
    sanitization, sanitizes description text, and enforces count/size caps.
    Returns ``(kept, skipped)`` where skipped entries are ``(name, reason)``.
    """
    kept: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    total_chars = 0
    for tool in tools:
        name = str(tool.get("name") or "")
        sanitized = sanitize_tool_name(name)
        if sanitized is None:
            skipped.append((name, "name is not a valid Python identifier"))
            continue
        if sanitized in seen:
            skipped.append((name, f"sanitized name {sanitized!r} collides with another tool"))
            continue
        if len(kept) >= MAX_TOOLS_PER_SERVER:
            skipped.append((name, f"server exceeds {MAX_TOOLS_PER_SERVER}-tool cap"))
            continue
        entry = {
            "name": name,
            "description": sanitize_tool_text(tool.get("description")),
            "input_schema": tool.get("input_schema") or {},
        }
        entry_chars = len(json.dumps(entry, ensure_ascii=False))
        if total_chars + entry_chars > MAX_SCHEMA_CHARS_PER_SERVER:
            skipped.append((name, "server exceeds total schema size cap"))
            continue
        seen.add(sanitized)
        total_chars += entry_chars
        kept.append(entry)
    return kept, skipped


async def discover_and_cache(
    workspace_id: str,
    sandbox: Any,
    servers: list[MCPServerConfig],
) -> list[dict[str, Any]]:
    """Discover ``servers`` inside ``sandbox``, sanitize, and cache snapshots.

    Each snapshot is cached under the server's own config fingerprint
    (``mcp_discovery_fingerprint``), not the workspace config version, so it
    survives unrelated mutations. Per-server error isolation: one broken server
    yields an ``error`` row and never blocks the others. A missing/stopped
    sandbox (or one predating the discovery driver) marks every server
    ``pending``. Returns the upserted ``workspace_mcp_tool_schemas`` rows.
    """
    rows: list[dict[str, Any]] = []
    discover = getattr(sandbox, "discover_user_mcp_schemas", None) if sandbox else None
    if discover is None:
        for server in servers:
            rows.append(
                await mcp_db.upsert_tool_schemas(
                    workspace_id, server.name, mcp_discovery_fingerprint(server),
                    status="pending",
                )
            )
        return rows

    try:
        results: dict[str, dict[str, Any]] = await discover(servers)
    except Exception as exc:
        logger.warning("[MCP_DISCOVERY] sandbox discovery failed for %s: %s", workspace_id, exc)
        results = {s.name: {"status": "error", "error": str(exc), "tools": []} for s in servers}

    for server in servers:
        fingerprint = mcp_discovery_fingerprint(server)
        result = results.get(server.name) or {
            "status": "error",
            "error": "no discovery result returned",
            "tools": [],
        }
        if result.get("status") != "ok":
            rows.append(
                await mcp_db.upsert_tool_schemas(
                    workspace_id,
                    server.name,
                    fingerprint,
                    status="error",
                    error=str(result.get("error") or "discovery failed")[:2000],
                )
            )
            continue
        kept, skipped = sanitize_discovered_tools(result.get("tools") or [])
        rows.append(
            await mcp_db.upsert_tool_schemas(
                workspace_id,
                server.name,
                fingerprint,
                tools=kept,
                status="ok",
                observed_meta={
                    "tool_count": len(kept),
                    "skipped": [list(item) for item in skipped],
                },
            )
        )
    return rows
