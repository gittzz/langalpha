"""Serper search tool for LangChain integration."""

import logging
from typing import Any, Dict, Literal, Optional, Union

import httpx
from langchain_core.tools import tool

from .serper import SerperAPI

logger = logging.getLogger(__name__)


def _normalize_time_range(time_range: Optional[str], default: Optional[str]) -> Optional[str]:
    """Normalize time range to single letter format."""
    if not time_range:
        return default

    time_map = {
        "hour": "h",
        "day": "d",
        "week": "w",
        "month": "m",
        "year": "y",
    }
    normalized = time_map.get(time_range.lower(), time_range.lower())

    if normalized in ("h", "d", "w", "m", "y"):
        return normalized

    logger.warning(f"Invalid time_range '{time_range}', ignoring")
    return default


def build_web_search_tool(
    max_results: int = 10,
    default_time_range: Optional[str] = None,
    verbose: bool = True,
    default_gl: str = "us",
    default_hl: str = "en",
):
    """Build a per-request Serper web_search tool.

    Each call returns a fresh tool whose settings live in closure scope, so
    concurrent requests with different settings can't race. The API wrapper is
    created lazily inside the tool call so a missing SERPER_API_KEY surfaces
    as a per-call error, not a build crash. ``verbose`` is accepted for the
    uniform builder interface; Serper results have no image variant.
    """
    state: Dict[str, Optional[SerperAPI]] = {"wrapper": None}

    def _get_api_wrapper() -> SerperAPI:
        if state["wrapper"] is None:
            state["wrapper"] = SerperAPI()
        return state["wrapper"]

    @tool(response_format="content_and_artifact")
    async def web_search(
        query: str,
        search_type: Optional[Literal["general", "news"]] = "general",
        time_range: Optional[Literal["h", "d", "w", "m", "y"]] = None,
        geographic_location: Optional[str] = None,
        language: Optional[str] = None,
    ) -> tuple[Union[list[dict[str, Any]], str], dict[str, Any]]:
        """Search the web for current information, news, and facts.

        Use when you need to:
        - Find recent news or current events
        - Look up facts, statistics, or real-time data
        - Research topics beyond your knowledge cutoff
        - Verify or update information

        Args:
            query: Search query to execute
            search_type: 'general' (default) or 'news' for news articles only
            time_range: Filter by recency - 'h' (hour), 'd' (day), 'w' (week), 'm' (month), 'y' (year)
            geographic_location: Country code (e.g., 'us', 'cn', 'uk')
            language: Language code (e.g., 'en', 'zh-cn')
        """
        try:
            api = _get_api_wrapper()
            serper_type = "news" if search_type == "news" else "search"
            effective_time_range = _normalize_time_range(time_range, default_time_range)
            gl = geographic_location or default_gl
            hl = language or default_hl

            logger.debug(
                f"Executing Serper search: query='{query}', "
                f"type={serper_type}, time_range={effective_time_range}, gl={gl}, hl={hl}"
            )

            detailed_results, metadata = await api.web_search(
                query=query,
                search_type=serper_type,
                num=max_results,
                time_range=effective_time_range,
                gl=gl,
                hl=hl,
            )

            logger.debug(f"Serper search completed: {len(detailed_results)} results returned")
            return detailed_results, metadata

        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            body = e.response.text if e.response is not None else "no response body"
            logger.error(f"Serper API HTTP {status}: {body}")
            error_message = f"Search failed (HTTP {status}): {body}"
            return error_message, {"error": error_message, "query": query}
        except httpx.HTTPError as e:
            logger.error(f"Serper search failed: {e}", exc_info=True)
            error_message = f"Search failed: {str(e)}"
            return error_message, {"error": str(e), "query": query}

    return web_search
