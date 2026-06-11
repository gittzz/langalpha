"""Tests for src/utils/tracking/infrastructure_costs.py — merged pricing table.

Search pricing now comes from src/tools/manifest/search_providers.json (per
depth level); providers.json `infrastructure_pricing` only carries non-search
leftovers. Tests assert key structure and relative behavior, not specific
credit numbers (tunable manifest data).
"""

from src.tools.search_manifest import get_search_providers
from src.utils.tracking.infrastructure_costs import (
    INFRASTRUCTURE_PRICING,
    _map_tool_to_service,
    calculate_infrastructure_credits,
    format_infrastructure_usage,
)


class TestPricingTable:
    def test_every_depth_has_a_qualified_key(self):
        for spec in get_search_providers().values():
            for depth in spec.depths:
                key = f"{spec.tracking_name}:{depth.name}"
                assert INFRASTRUCTURE_PRICING[key]["credits_per_use"] == depth.credits_per_use

    def test_bare_key_prices_at_default_depth(self):
        """Legacy/unqualified usage counts bill at the provider's default level."""
        for spec in get_search_providers().values():
            bare = INFRASTRUCTURE_PRICING[spec.tracking_name]
            assert bare["credits_per_use"] == spec.default_depth_spec.credits_per_use

    def test_auxiliary_tools_priced(self):
        for key in ("TavilySearchImages", "TavilyResearchMini", "TavilyResearchPro"):
            assert INFRASTRUCTURE_PRICING[key]["credits_per_use"] > 0


class TestServiceMapping:
    def test_depth_suffix_stripped(self):
        assert _map_tool_to_service("TavilySearchTool:deep") == "tavily_search"
        assert _map_tool_to_service("TavilySearchTool:standard") == "tavily_search"

    def test_bare_key_unchanged(self):
        assert _map_tool_to_service("TavilySearchTool") == "tavily_search"
        assert _map_tool_to_service("SerperSearchTool") == "serper_search"

    def test_unknown_tool_lowercases_base(self):
        assert _map_tool_to_service("FutureTool:deep") == "futuretool"


class TestCalculateCredits:
    def test_qualified_key_bills_that_depth(self):
        tavily = get_search_providers()["tavily"]
        deep = tavily.depth("deep")
        result = calculate_infrastructure_credits({"TavilySearchTool:deep": 3})
        assert result["total_credits"] == 3 * deep.credits_per_use
        assert result["services"]["tavily_search"]["usage_count"] == 3

    def test_deep_costs_more_than_standard(self):
        """The per-depth pricing fix: deep and standard bill differently."""
        deep = calculate_infrastructure_credits({"TavilySearchTool:deep": 1})
        standard = calculate_infrastructure_credits({"TavilySearchTool:standard": 1})
        assert deep["total_credits"] > standard["total_credits"]

    def test_mixed_depths_aggregate_per_service(self):
        """Qualified + bare keys share a service entry, summed not overwritten."""
        result = calculate_infrastructure_credits(
            {"TavilySearchTool:deep": 2, "TavilySearchTool": 1}
        )
        entry = result["services"]["tavily_search"]
        assert entry["usage_count"] == 3
        tavily = get_search_providers()["tavily"]
        expected = (
            2 * tavily.depth("deep").credits_per_use
            + 1 * tavily.default_depth_spec.credits_per_use
        )
        assert result["total_credits"] == expected

    def test_unknown_tool_skipped(self):
        result = calculate_infrastructure_credits({"NoSuchTool": 5})
        assert result["total_credits"] == 0.0
        assert result["services"] == {}

    def test_mixed_rates_omit_credits_per_use(self):
        """When depths with different rates share a service entry, the
        per-use figure is omitted rather than reporting the last key's rate."""
        result = calculate_infrastructure_credits(
            {"TavilySearchTool:deep": 2, "TavilySearchTool:standard": 1}
        )
        entry = result["services"]["tavily_search"]
        assert "credits_per_use" not in entry
        # Order-independent: reversed insertion omits it too.
        reversed_result = calculate_infrastructure_credits(
            {"TavilySearchTool:standard": 1, "TavilySearchTool:deep": 2}
        )
        assert "credits_per_use" not in reversed_result["services"]["tavily_search"]
        assert reversed_result["total_credits"] == result["total_credits"]

    def test_uniform_rate_keeps_credits_per_use(self):
        """Same-rate keys aggregating into one service keep the figure."""
        tavily = get_search_providers()["tavily"]
        result = calculate_infrastructure_credits({"TavilySearchTool:deep": 2})
        entry = result["services"]["tavily_search"]
        assert entry["credits_per_use"] == tavily.depth("deep").credits_per_use


class TestFormatUsage:
    def test_type_is_depth_name(self):
        formatted = format_infrastructure_usage({"TavilySearchTool:deep": 2})
        assert formatted["services"]["tavily_search"] == {"count": 2, "type": "deep"}

    def test_counts_aggregate_across_keys(self):
        formatted = format_infrastructure_usage(
            {"TavilySearchTool:deep": 2, "TavilySearchTool:standard": 1}
        )
        assert formatted["services"]["tavily_search"]["count"] == 3
        # Dominant count's depth wins the type field.
        assert formatted["services"]["tavily_search"]["type"] == "deep"

    def test_dominant_type_is_insertion_order_independent(self):
        a = format_infrastructure_usage(
            {"TavilySearchTool:deep": 2, "TavilySearchTool:standard": 1}
        )
        b = format_infrastructure_usage(
            {"TavilySearchTool:standard": 1, "TavilySearchTool:deep": 2}
        )
        assert (
            a["services"]["tavily_search"]["type"]
            == b["services"]["tavily_search"]["type"]
            == "deep"
        )

    def test_dominant_type_three_way_collision(self):
        formatted = format_infrastructure_usage(
            {
                "TavilySearchTool:deep": 2,
                "TavilySearchTool:standard": 1,
                "TavilySearchTool:fast": 4,
            }
        )
        entry = formatted["services"]["tavily_search"]
        assert entry["count"] == 7
        assert entry["type"] == "fast"

    def test_auxiliary_tools_keep_type_field(self):
        """Research/image rows keep their pre-migration JSONB type values."""
        formatted = format_infrastructure_usage(
            {"TavilyResearchMini": 1, "TavilyResearchPro": 1, "TavilySearchImages": 1}
        )
        services = formatted["services"]
        assert services["tavily_research_mini"]["type"] == "research_mini"
        assert services["tavily_research_pro"]["type"] == "research_pro"
        assert services["tavily_images"]["type"] == "advanced"
