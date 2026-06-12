"""Server-side HTML→PDF rendering via headless Chromium (Playwright).

Renders a workspace HTML document — loaded from the server's own loopback
``/api/v1/wsfiles/...`` route — into PDF bytes. Playwright is imported lazily
inside functions so this module never fails to import without the ``pdf`` extra;
callers map ``PdfRenderUnavailable`` to a 501.

SSRF containment: every subresource request is gated by ``_is_request_allowed``,
which permits only the workspace's own serve prefix plus a fixed https CDN
allowlist. Agent-authored HTML therefore cannot reach internal services or
cloud metadata endpoints from inside the server.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# --- Error taxonomy --------------------------------------------------------


class PdfRenderError(Exception):
    """Rendering failed for a reason other than timeout/unavailability."""


class PdfRenderUnavailable(PdfRenderError):
    """Playwright or its Chromium binary is not installed."""


class PdfRenderTimeout(PdfRenderError):
    """Rendering exceeded the allotted time budget."""


# --- SSRF allowlist --------------------------------------------------------

# https-only CDN hosts agent HTML may pull libraries/fonts from. Everything
# else (internal hosts, metadata IPs, lookalike domains) is aborted.
_CDN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "esm.sh",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
    }
)


def _is_request_allowed(url: str, workspace_serve_prefix: str) -> bool:
    """True if ``url`` may be fetched during a render.

    Allowed when the URL starts with this workspace's serve prefix, or when its
    host is an exact match in the https CDN allowlist. Exact host equality (not
    suffix) blocks lookalikes like ``unpkg.com.evil.io``.
    """
    if url.startswith(workspace_serve_prefix):
        return True
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    return parsed.hostname in _CDN_ALLOWLIST


# --- Browser lifecycle -----------------------------------------------------

# Cap concurrent renders: each holds a Chromium context + page.
_RENDER_SEMAPHORE = asyncio.Semaphore(2)

# Total per-render budget (navigation + pdf). Networkidle wait is bounded
# below this so a slow asset can fall back to "load" and still finish.
_RENDER_TIMEOUT_MS = 30_000
_GOTO_TIMEOUT_MS = 20_000

# Letter paper at CSS 96dpi (8.5x11in). The viewport must match the print
# width so responsive layouts and chart canvases render at paper size from
# the start — otherwise Chart.js rasterizes at screen width and printToPDF
# snapshots stale, oversized bitmaps over the reflowed layout.
_PRINT_VIEWPORT = {"width": 816, "height": 1056}

# Post-load settle before snapshotting: lets ResizeObserver-driven redraws
# (Chart.js et al.) finish after print-media layout.
_SETTLE_MS = 300

_EXECUTABLE_MISSING_HINT = "executable doesn't exist"

_browser = None
_playwright_cm = None
_browser_lock = asyncio.Lock()


async def _get_browser():
    """Return the singleton headless Chromium, launching it once.

    Raises ``PdfRenderUnavailable`` if Playwright or the Chromium binary is
    missing — both are optional and only required to serve ``?format=pdf``.
    """
    global _browser, _playwright_cm
    if _browser is not None:
        return _browser
    async with _browser_lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PdfRenderUnavailable("playwright is not installed") from exc
        try:
            cm = async_playwright()
            pw = await cm.start()
            browser = await pw.chromium.launch(headless=True)
        except PlaywrightError as exc:
            if _EXECUTABLE_MISSING_HINT in str(exc).lower():
                raise PdfRenderUnavailable(
                    "Chromium is not installed (run: playwright install chromium)"
                ) from exc
            raise PdfRenderError(f"Failed to launch Chromium: {exc}") from exc
        _playwright_cm = cm
        _browser = browser
        return _browser


async def render_workspace_pdf(internal_url: str, *, workspace_serve_prefix: str) -> bytes:
    """Render a workspace HTML URL to PDF bytes in headless Chromium.

    ``internal_url`` is the server's own loopback wsfiles URL; subresource
    requests are SSRF-gated to ``workspace_serve_prefix`` plus the CDN
    allowlist. Raises ``PdfRenderUnavailable`` / ``PdfRenderTimeout`` /
    ``PdfRenderError`` for the corresponding failure classes.
    """
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    browser = await _get_browser()

    async with _RENDER_SEMAPHORE:
        context = await browser.new_context(viewport=_PRINT_VIEWPORT)
        try:

            async def _route_handler(route, request):
                if _is_request_allowed(request.url, workspace_serve_prefix):
                    await route.continue_()
                else:
                    await route.abort()

            page = await context.new_page()
            await page.route("**/*", _route_handler)
            # Lay out with print CSS from the first paint so charts size for
            # paper instead of re-rendering during the print reflow.
            await page.emulate_media(media="print")

            async def _render() -> bytes:
                try:
                    await page.goto(
                        internal_url,
                        wait_until="networkidle",
                        timeout=_GOTO_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError:
                    # Networkidle never settled (long-polling asset, etc.) —
                    # fall back to a plain load and render what we have.
                    await page.goto(internal_url, wait_until="load", timeout=_GOTO_TIMEOUT_MS)
                await page.evaluate(
                    "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
                )
                await page.wait_for_timeout(_SETTLE_MS)
                return await page.pdf(print_background=True, prefer_css_page_size=True)

            try:
                return await asyncio.wait_for(_render(), timeout=_RENDER_TIMEOUT_MS / 1000)
            except (asyncio.TimeoutError, PlaywrightTimeoutError) as exc:
                raise PdfRenderTimeout("PDF rendering timed out") from exc
            except PdfRenderError:
                raise
            except PlaywrightError as exc:
                raise PdfRenderError(f"PDF rendering failed: {exc}") from exc
        finally:
            await context.close()
