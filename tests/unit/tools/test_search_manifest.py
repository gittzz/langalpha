"""Tests for src/tools/search_manifest.py — manifest loading and tier resolution.

Per project convention, these test structure and fallback *behavior*, not
specific tier/credit numbers (tunable manifest data).
"""

import pytest

from src.tools.search_manifest import (
    DepthSpec,
    get_auxiliary_search_pricing,
    get_search_provider_spec,
    get_search_providers,
    resolve_depth_tier,
    resolve_provider_tier,
)


class TestManifestLoading:
    def test_loads_known_providers(self):
        providers = get_search_providers()
        assert {"tavily", "serper", "bocha"} <= set(providers)

    def test_depths_are_ordered_and_self_describing(self):
        """Tavily's levels arrive in manifest (fastest → deepest) order."""
        tavily = get_search_providers()["tavily"]
        assert [d.name for d in tavily.depths] == ["ultra_fast", "fast", "standard", "deep"]
        for d in tavily.depths:
            assert d.display_name
            assert isinstance(d.native_params, dict)
            assert d.credits_per_use > 0

    def test_default_depth_is_a_declared_level(self):
        for spec in get_search_providers().values():
            assert spec.depth(spec.default_depth) is not None
            assert spec.default_depth_spec.name == spec.default_depth

    def test_depth_names_unique_per_provider(self):
        for spec in get_search_providers().values():
            names = [d.name for d in spec.depths]
            assert len(names) == len(set(names))

    def test_every_provider_has_a_tracking_name(self):
        for spec in get_search_providers().values():
            assert spec.tracking_name

    def test_depth_lookup_unknown_returns_none(self):
        tavily = get_search_providers()["tavily"]
        assert tavily.depth("does-not-exist") is None
        assert tavily.depth(None) is None

    def test_unknown_provider_spec_is_none(self):
        assert get_search_provider_spec("exa-not-yet") is None

    def test_auxiliary_pricing_present(self):
        aux = get_auxiliary_search_pricing()
        assert {"TavilySearchImages", "TavilyResearchMini", "TavilyResearchPro"} <= set(aux)
        for entry in aux.values():
            assert entry["credits_per_use"] > 0


class TestTierResolution:
    def test_null_min_tier_falls_back_to_env_floor(self, monkeypatch):
        """min_tier null resolves to the SEARCH_PROVIDER_MIN_TIER floor."""
        monkeypatch.setattr("src.config.settings.SEARCH_PROVIDER_MIN_TIER", 7)
        spec = get_search_providers()["tavily"]
        if spec.min_tier is None:
            assert resolve_provider_tier(spec) == 7
        for d in spec.depths:
            if d.min_tier is None:
                assert resolve_depth_tier(d) == 7

    def test_explicit_min_tier_wins_over_floor(self, monkeypatch):
        """An entry's explicit min_tier beats the env floor."""
        monkeypatch.setattr("src.config.settings.SEARCH_PROVIDER_MIN_TIER", 7)
        depth = DepthSpec(
            name="deep", display_name="Deep", native_params={}, min_tier=2, credits_per_use=16
        )
        assert resolve_depth_tier(depth) == 2

    def test_explicit_zero_tier_is_respected(self, monkeypatch):
        """min_tier 0 (free) must not be treated as falsy/unset."""
        monkeypatch.setattr("src.config.settings.SEARCH_PROVIDER_MIN_TIER", 7)
        depth = DepthSpec(
            name="fast", display_name="Fast", native_params={}, min_tier=0, credits_per_use=8
        )
        assert resolve_depth_tier(depth) == 0


class TestManifestValidation:
    def test_duplicate_depth_names_rejected(self, monkeypatch):
        import src.tools.search_manifest as sm

        bad = {
            "providers": {
                "x": {
                    "tracking_name": "XTool",
                    "default_depth": "a",
                    "depths": [
                        {"name": "a", "credits_per_use": 1},
                        {"name": "a", "credits_per_use": 2},
                    ],
                }
            }
        }
        monkeypatch.setattr(sm, "_load_manifest", lambda: bad)
        sm.get_search_providers.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="duplicate depth names"):
                sm.get_search_providers()
        finally:
            sm.get_search_providers.cache_clear()

    def test_default_depth_must_exist(self, monkeypatch):
        import src.tools.search_manifest as sm

        bad = {
            "providers": {
                "x": {
                    "tracking_name": "XTool",
                    "default_depth": "missing",
                    "depths": [{"name": "a", "credits_per_use": 1}],
                }
            }
        }
        monkeypatch.setattr(sm, "_load_manifest", lambda: bad)
        sm.get_search_providers.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="default_depth"):
                sm.get_search_providers()
        finally:
            sm.get_search_providers.cache_clear()
