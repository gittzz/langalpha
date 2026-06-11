"""Search provider manifest: depth levels, access tiers, and per-call pricing.

Data-only module (stdlib + src.config) — it must never import search.py,
langchain, or the provider packages, so the resolve-time gate and write
validation can import it cheaply. Entries with ``min_tier: null`` resolve
against the SEARCH_PROVIDER_MIN_TIER env floor.
"""

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).parent / "manifest" / "search_providers.json"


@dataclass(frozen=True)
class DepthSpec:
    """One depth level a provider offers (ordered fastest → deepest)."""

    name: str
    display_name: str
    native_params: Dict[str, Any]
    min_tier: Optional[int]
    credits_per_use: float


@dataclass(frozen=True)
class SearchProviderSpec:
    """A web-search provider entry from the manifest."""

    name: str
    display_name: str
    tracking_name: str
    min_tier: Optional[int]
    default_depth: str
    depths: Tuple[DepthSpec, ...]

    def depth(self, name: Optional[str]) -> Optional[DepthSpec]:
        """Look up a depth level by name; None if the provider doesn't offer it."""
        for d in self.depths:
            if d.name == name:
                return d
        return None

    @property
    def default_depth_spec(self) -> DepthSpec:
        spec = self.depth(self.default_depth)
        assert spec is not None  # guaranteed by _load_manifest validation
        return spec


def _load_manifest() -> Dict[str, Any]:
    if not _MANIFEST_PATH.exists():
        raise RuntimeError(f"Search provider manifest not found at {_MANIFEST_PATH}")
    try:
        with open(_MANIFEST_PATH) as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to load search provider manifest {_MANIFEST_PATH}: {e}")


@lru_cache(maxsize=1)
def get_search_providers() -> Mapping[str, SearchProviderSpec]:
    """Load and validate the provider specs, keyed by provider name.

    Read-only mapping: the lru_cache shares one object across all callers.
    """
    manifest = _load_manifest()
    providers: Dict[str, SearchProviderSpec] = {}

    for name, entry in manifest.get("providers", {}).items():
        depths = tuple(
            DepthSpec(
                name=d["name"],
                display_name=d.get("display_name", d["name"]),
                native_params=d.get("native_params", {}),
                min_tier=d.get("min_tier"),
                credits_per_use=d["credits_per_use"],
            )
            for d in entry.get("depths", [])
        )
        spec = SearchProviderSpec(
            name=name,
            display_name=entry.get("display_name", name),
            tracking_name=entry["tracking_name"],
            min_tier=entry.get("min_tier"),
            default_depth=entry["default_depth"],
            depths=depths,
        )

        if not depths:
            raise RuntimeError(f"Search provider {name!r} declares no depth levels")
        depth_names = [d.name for d in depths]
        if len(depth_names) != len(set(depth_names)):
            raise RuntimeError(f"Search provider {name!r} has duplicate depth names")
        if spec.depth(spec.default_depth) is None:
            raise RuntimeError(
                f"Search provider {name!r} default_depth {spec.default_depth!r} "
                f"is not one of its depth levels {depth_names}"
            )
        providers[name] = spec

    if not providers:
        raise RuntimeError(f"No search providers defined in {_MANIFEST_PATH}")
    return MappingProxyType(providers)


def get_search_provider_spec(name: str) -> Optional[SearchProviderSpec]:
    """Spec for a provider name, or None if unknown."""
    return get_search_providers().get(name)


@lru_cache(maxsize=1)
def get_auxiliary_search_pricing() -> Mapping[str, Dict[str, Any]]:
    """Pricing entries for search tools that aren't depth-selectable providers
    (image search, research) — consumed by the infrastructure billing table."""
    return MappingProxyType(_load_manifest().get("auxiliary_tools", {}))


def _tier_floor() -> int:
    # Read at call time so tests monkeypatching src.config.settings see effect.
    from src.config import settings

    return settings.SEARCH_PROVIDER_MIN_TIER


def resolve_provider_tier(spec: SearchProviderSpec) -> int:
    """Effective min tier for choosing this provider (env floor when unset)."""
    return spec.min_tier if spec.min_tier is not None else _tier_floor()


def resolve_depth_tier(depth: DepthSpec) -> int:
    """Effective min tier for choosing this depth level (env floor when unset)."""
    return depth.min_tier if depth.min_tier is not None else _tier_floor()
