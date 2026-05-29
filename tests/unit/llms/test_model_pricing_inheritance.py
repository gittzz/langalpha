"""Pricing resolution for the new model entries via the canonical
find_model_pricing() path used by cost tracking.

Variant providers (OAuth) carry no pricing of their own and inherit the
parent provider's entry; region variants keep their own rates.
"""

from src.llms.pricing_utils import find_model_pricing


class TestModelPricingResolution:
    def test_anthropic_direct_pricing(self):
        pricing = find_model_pricing("claude-opus-4-8", provider="anthropic")
        assert pricing is not None
        assert pricing["input"] == 5.0
        assert pricing["output"] == 25.0

    def test_oauth_variant_inherits_parent_pricing(self):
        """claude-oauth has no pricing list; inherits anthropic for the same id."""
        pricing = find_model_pricing("claude-opus-4-8", provider="claude-oauth")
        assert pricing is not None
        assert pricing["input"] == 5.0
        assert pricing["output"] == 25.0

    def test_qwen_cn_pricing(self):
        pricing = find_model_pricing("qwen3.7-max", provider="dashscope")
        assert pricing is not None
        assert pricing["input"] == 1.714

    def test_intl_variant_keeps_own_pricing_not_parent(self):
        """dashscope-intl has its own pricing list, so it must NOT inherit the
        cheaper CN rates from the parent dashscope provider."""
        cn = find_model_pricing("qwen3.7-max", provider="dashscope")
        intl = find_model_pricing("qwen3.7-max", provider="dashscope-intl")
        assert cn["input"] == 1.714
        assert intl["input"] == 2.677
        assert cn["input"] != intl["input"]

    def test_unknown_model_returns_none(self):
        assert find_model_pricing("does-not-exist", provider="anthropic") is None
