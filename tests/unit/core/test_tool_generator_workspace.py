"""Tests for user (workspace) MCP server codegen in tool_generator.

Covers vault-only secret resolution (no os.environ fallback), per-server env
scoping, http/sse header injection, the discover() output shape, no-vault
discovery, and builtin byte-stability invariants.
"""

import ast
import json
import os

import pytest

from ptc_agent.config.core import MCPServerConfig
from ptc_agent.core.mcp_sanitize import VAULT_REF_RE
from ptc_agent.core.tool_generator import ToolFunctionGenerator


def _exec_client(code: str) -> dict:
    """Compile + exec generated client source, returning its namespace."""
    ast.parse(code)  # must be valid Python
    ns: dict = {}
    exec(compile(code, "gen_mcp_client", "exec"), ns)  # noqa: S102 - testing generated code
    return ns


def _write_vault(tmp_path, secrets: dict) -> str:
    """Write a vault file under tmp_path/_internal and return the working dir."""
    internal = tmp_path / "_internal"
    internal.mkdir(parents=True, exist_ok=True)
    (internal / ".vault_secrets.json").write_text(json.dumps(secrets))
    return str(tmp_path)


class TestBuiltinByteStability:
    """Builtin-only codegen must not gain vault machinery."""

    def test_no_vault_refs_in_builtin_client(self):
        gen = ToolFunctionGenerator()
        servers = [
            MCPServerConfig(
                name="data_srv",
                transport="stdio",
                command="node",
                args=["srv.js"],
                env={"PLACEHOLDER_KEY": "x"},
            ),
            MCPServerConfig(
                name="remote_srv", transport="sse", url="https://example.test/mcp"
            ),
        ]
        code = gen.generate_mcp_client_code(servers)
        # No vault resolution helpers leak into builtin-only output.
        assert "_load_vault" not in code
        assert "_VAULT_SECRETS_FILE" not in code
        assert "_build_proc_env" not in code
        assert "def discover(" not in code

    def test_builtin_stdio_uses_os_environ(self):
        gen = ToolFunctionGenerator()
        servers = [
            MCPServerConfig(
                name="data_srv", transport="stdio", command="node", args=["srv.js"]
            )
        ]
        code = gen.generate_mcp_client_code(servers)
        # Builtin env resolution still reads os.environ.
        assert "os.environ.copy()" in code
        assert 'for key in config.get("env_keys", []):' in code


class TestVaultOnlyResolution:
    """Workspace servers resolve secrets vault-only, no host-env fallback."""

    def test_no_os_environ_fallback_for_workspace_secret(self, tmp_path):
        workdir = _write_vault(tmp_path, {"USER_TOKEN": "resolved-secret"})
        gen = ToolFunctionGenerator()
        server = MCPServerConfig(
            name="user_srv",
            transport="stdio",
            command="npx",
            args=["-y", "@scope/pkg"],
            env={
                "TOKEN": "${vault:USER_TOKEN}",
                # A bare ${VAR} naming a platform var must NOT resolve from host env.
                "LEAK": "${PLATFORM_TOKEN}",
                "LITERAL": "plain",
            },
            source="workspace",
        )
        code = gen.generate_mcp_client_code([server], working_dir=workdir)
        ns = _exec_client(code)

        os.environ["PLATFORM_TOKEN"] = "must-not-leak"
        try:
            env = ns["_build_proc_env"](ns["_SERVER_CONFIGS"]["user_srv"], "user_srv")
        finally:
            del os.environ["PLATFORM_TOKEN"]

        assert env["TOKEN"] == "resolved-secret"
        assert env["LITERAL"] == "plain"
        # Bare ${VAR} is left as an inert placeholder, never host-resolved.
        assert env["LEAK"] == "${PLATFORM_TOKEN}"
        assert "must-not-leak" not in json.dumps(env)

    def test_missing_secret_raises_naming_secret_not_value(self, tmp_path):
        workdir = _write_vault(tmp_path, {})  # empty vault
        gen = ToolFunctionGenerator()
        server = MCPServerConfig(
            name="user_srv",
            transport="stdio",
            command="npx",
            args=["x"],
            env={"TOKEN": "${vault:NEEDED_NAME}"},
            source="workspace",
        )
        ns = _exec_client(gen.generate_mcp_client_code([server], working_dir=workdir))
        with pytest.raises(RuntimeError) as exc:
            ns["_build_proc_env"](ns["_SERVER_CONFIGS"]["user_srv"], "user_srv")
        assert "NEEDED_NAME" in str(exc.value)


class TestPerServerScoping:
    """Workspace stdio env is minimal — never the full os.environ."""

    def test_env_scoped_to_declared_values(self, tmp_path):
        workdir = _write_vault(tmp_path, {"USER_TOKEN": "s"})
        gen = ToolFunctionGenerator()
        server = MCPServerConfig(
            name="user_srv",
            transport="stdio",
            command="npx",
            args=["x"],
            env={"TOKEN": "${vault:USER_TOKEN}"},
            source="workspace",
        )
        ns = _exec_client(gen.generate_mcp_client_code([server], working_dir=workdir))

        os.environ["SOME_UNRELATED_HOST_VAR"] = "secret-host-value"
        try:
            env = ns["_build_proc_env"](ns["_SERVER_CONFIGS"]["user_srv"], "user_srv")
        finally:
            del os.environ["SOME_UNRELATED_HOST_VAR"]

        # Full host env is not handed to the untrusted subprocess.
        assert "SOME_UNRELATED_HOST_VAR" not in env
        # Only declared + safe-base + PYTHONPATH keys present.
        declared = {
            k
            for k in env
            if k not in ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH")
        }
        assert declared == {"TOKEN"}


class TestHeaderInjection:
    """Workspace sse/http servers send vault-resolved headers."""

    def test_url_and_headers_resolved(self, tmp_path):
        workdir = _write_vault(tmp_path, {"USER_TOKEN": "abc123"})
        gen = ToolFunctionGenerator()
        server = MCPServerConfig(
            name="user_http",
            transport="http",
            url="https://example.test/${vault:USER_TOKEN}",
            headers={"Authorization": "Bearer ${vault:USER_TOKEN}"},
            source="workspace",
        )
        ns = _exec_client(gen.generate_mcp_client_code([server], working_dir=workdir))
        url, headers = ns["_resolve_sse"](
            ns["_SERVER_CONFIGS"]["user_http"], "user_http"
        )
        assert url == "https://example.test/abc123"
        assert headers["Authorization"] == "Bearer abc123"


class TestNoVaultDiscovery:
    """Discovery tolerates a missing vault file (inert placeholders)."""

    def test_stdio_env_placeholder_when_no_vault(self, tmp_path):
        # No vault file written at all.
        workdir = str(tmp_path)
        (tmp_path / "_internal").mkdir()
        gen = ToolFunctionGenerator()
        server = MCPServerConfig(
            name="user_srv",
            transport="stdio",
            command="npx",
            args=["x"],
            env={"TOKEN": "${vault:USER_TOKEN}"},
            source="workspace",
        )
        ns = _exec_client(gen.generate_mcp_client_code([server], working_dir=workdir))
        env = ns["_build_proc_env"](
            ns["_SERVER_CONFIGS"]["user_srv"], "user_srv", discovery=True
        )
        # Discovery substitutes inert empty string, never raises.
        assert env["TOKEN"] == ""

    def test_http_header_placeholder_when_no_vault(self, tmp_path):
        workdir = str(tmp_path)
        (tmp_path / "_internal").mkdir()
        gen = ToolFunctionGenerator()
        server = MCPServerConfig(
            name="user_http",
            transport="http",
            url="https://example.test/mcp",
            headers={"Authorization": "Bearer ${vault:USER_TOKEN}"},
            source="workspace",
        )
        ns = _exec_client(gen.generate_mcp_client_code([server], working_dir=workdir))
        _url, headers = ns["_resolve_sse"](
            ns["_SERVER_CONFIGS"]["user_http"], "user_http", discovery=True
        )
        assert headers["Authorization"] == "Bearer "


class TestDiscoverEntrypoint:
    """discover() shape + presence."""

    def test_discover_present_and_compiles_for_workspace(self, tmp_path):
        workdir = _write_vault(tmp_path, {})
        gen = ToolFunctionGenerator()
        server = MCPServerConfig(
            name="user_srv",
            transport="stdio",
            command="npx",
            args=["x"],
            source="workspace",
        )
        code = gen.generate_mcp_client_code([server], working_dir=workdir)
        assert "def discover(" in code
        assert '__name__ == "__main__"' in code
        ns = _exec_client(code)
        # Unknown server returns the structured error shape, never raises.
        res = ns["discover"]("does_not_exist")
        assert res == {
            "server": "does_not_exist",
            "status": "error",
            "error": "unknown server",
            "tools": [],
        }


class TestGeneratedRegexMirrorsConstant:
    """The in-sandbox vault regex must match mcp_sanitize.VAULT_REF_RE."""

    def test_pattern_in_sync(self, tmp_path):
        workdir = _write_vault(tmp_path, {})
        gen = ToolFunctionGenerator()
        server = MCPServerConfig(
            name="user_srv",
            transport="stdio",
            command="npx",
            args=["x"],
            source="workspace",
        )
        ns = _exec_client(gen.generate_mcp_client_code([server], working_dir=workdir))
        assert ns["_VAULT_REF_RE"].pattern == VAULT_REF_RE.pattern


class TestWorkspaceToolTextSanitized:
    """Workspace tool text is sanitized in wrappers; builtins unchanged."""

    def test_workspace_docstring_neutralizes_breakout(self):
        from ptc_agent.core.mcp_registry import MCPToolInfo

        evil_desc = 'desc """ injected """ tail'
        tool = MCPToolInfo(
            name="probe",
            description=evil_desc,
            input_schema={"type": "object", "properties": {}},
            server_name="user_srv",
        )
        gen = ToolFunctionGenerator()
        module = gen.generate_tool_module("user_srv", [tool], source="workspace")
        # The generated module must compile — the breakout is inert.
        ast.parse(module)

    def test_builtin_text_unchanged(self):
        from ptc_agent.core.mcp_registry import MCPToolInfo

        tool = MCPToolInfo(
            name="probe",
            description="A plain builtin description.",
            input_schema={"type": "object", "properties": {}},
            server_name="srv",
        )
        gen = ToolFunctionGenerator()
        module = gen.generate_tool_module("srv", [tool])
        assert "A plain builtin description." in module
