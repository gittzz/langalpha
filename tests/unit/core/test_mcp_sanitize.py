"""Tests for ptc_agent.core.mcp_sanitize.

Covers the vault-reference regex, identifier sanitization + collision
detection, and untrusted-text neutralization for user MCP servers.
"""

import ast

from ptc_agent.core.mcp_sanitize import (
    VAULT_REF_RE,
    sanitize_tool_name,
    sanitize_tool_set,
    sanitize_tool_text,
    vault_refs,
)


class _Tool:
    """Minimal stand-in for MCPToolInfo (only ``.name`` is read)."""

    def __init__(self, name: str) -> None:
        self.name = name


class TestVaultRefRegex:
    """Tests for VAULT_REF_RE / vault_refs."""

    def test_matches_vault_form_only(self):
        assert vault_refs("${vault:ALPHA} and ${vault:BETA}") == ["ALPHA", "BETA"]

    def test_bare_var_is_not_a_vault_ref(self):
        # A plain ${VAR} must NOT be a vault reference — this is what stops a
        # user from naming a platform env var and having it resolve.
        assert vault_refs("${PLATFORM_TOKEN}") == []
        assert VAULT_REF_RE.findall("${PLATFORM_TOKEN}") == []

    def test_empty_and_none(self):
        assert vault_refs("") == []
        assert vault_refs(None) == []

    def test_rejects_illegal_secret_name_chars(self):
        assert vault_refs("${vault:bad-name}") == []
        assert vault_refs("${vault:ok_name}") == ["ok_name"]


class TestSanitizeToolName:
    """Tests for sanitize_tool_name."""

    def test_dash_and_dot_collapse_to_underscore(self):
        assert sanitize_tool_name("foo-bar") == "foo_bar"
        assert sanitize_tool_name("foo.bar") == "foo_bar"

    def test_leading_digit_prefixed(self):
        assert sanitize_tool_name("2cool") == "_2cool"

    def test_keyword_suffixed(self):
        assert sanitize_tool_name("class") == "class_"
        assert sanitize_tool_name("for") == "for_"

    def test_unsalvageable_returns_none(self):
        assert sanitize_tool_name("") is None
        assert sanitize_tool_name("!!!") is None
        assert sanitize_tool_name("---") is None

    def test_result_is_valid_identifier(self):
        for raw in ("a/b", "weird name", "tool@1"):
            out = sanitize_tool_name(raw)
            assert out is not None
            assert out.isidentifier()


class TestSanitizeToolSet:
    """Tests for sanitize_tool_set collision detection."""

    def test_collision_first_wins_and_records_reason(self):
        result = sanitize_tool_set([_Tool("foo-bar"), _Tool("foo.bar")])
        assert [t.name for t in result.kept] == ["foo-bar"]
        assert len(result.skipped) == 1
        skipped_name, reason = result.skipped[0]
        assert skipped_name == "foo.bar"
        assert "collides" in reason

    def test_illegal_name_skipped_with_reason(self):
        result = sanitize_tool_set([_Tool("ok"), _Tool("!!!")])
        assert [t.name for t in result.kept] == ["ok"]
        assert result.skipped[0][0] == "!!!"
        assert "identifier" in result.skipped[0][1]


class TestSanitizeToolText:
    """Tests for sanitize_tool_text."""

    def test_triple_quote_breakout_rendered_inert(self):
        evil = 'safe """ \nimport os; os.system("x") """ tail'
        cleaned = sanitize_tool_text(evil)
        # Embedding the cleaned text in a docstring must still compile — the
        # injected triple-quotes cannot terminate the docstring early.
        module = f'def f():\n    """{cleaned}"""\n    pass\n'
        ast.parse(module)

    def test_strips_control_chars(self):
        assert "\x00" not in sanitize_tool_text("a\x00b\x07c")
        # tab/newline survive
        assert sanitize_tool_text("a\tb\nc") == "a\tb\nc"

    def test_length_cap(self):
        capped = sanitize_tool_text("x" * 5000, max_len=100)
        assert capped.startswith("x" * 100)
        assert "truncated" in capped

    def test_empty_and_none(self):
        assert sanitize_tool_text("") == ""
        assert sanitize_tool_text(None) == ""
