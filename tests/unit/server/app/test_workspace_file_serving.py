"""Unit tests for the unauthenticated workspace file-serving endpoint.

Calls ``serve_workspace_file`` / ``serve_workspace_file_endpoint`` directly
(same style as ``test_workspace_files_routing.py``) so no TestClient is
needed. The serving core resolves a workspace by UUID, reads bytes from the
live sandbox or the DB fallback, applies the sandboxed CSP, redacts vault
secrets from text bodies, and optionally splices a theme-sync script into HTML.

Covered: MIME mapping, uniform-404 (unknown workspace / missing file /
traversal), DB fallback (text + binary), CSP header on every response,
redaction of text bodies, ``?inject=theme`` splicing for HTML only, and
byte-faithful plain GET.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.server.app.workspace_files import (
    _guess_content_type,
    _has_traversal,
    serve_workspace_file,
    serve_workspace_file_endpoint,
)
from src.server.services import pdf_render

WS_ID = "ws-test-0001"
OWNER = "user-test-1"
_VAULT_PATCH = "src.server.app.workspace_files.get_vault_secrets_for_redaction"
_DBWS_PATCH = "src.server.app.workspace_files.db_get_workspace"
_FP_PATCH = "src.server.app.workspace_files.FilePersistenceService"
_WD_PATCH = "src.server.app.workspace_files._get_work_dir"
_SANDBOX_PATCH = "src.server.app.workspace_files._acquire_sandbox"
_RENDER_PATCH = "src.server.services.pdf_render.render_workspace_pdf"
_PDF_INTERNAL_BASE = "http://127.0.0.1:8000"


def _workspace(status: str) -> dict:
    return {
        "workspace_id": WS_ID,
        "user_id": OWNER,
        "status": status,
        "config": None,
        "sandbox_id": "sb-existing",
    }


def _db_text_record(text: str, mime: str | None = "text/html") -> dict:
    return {
        "file_name": "report.html",
        "content_text": text,
        "content_binary": None,
        "is_binary": False,
        "mime_type": mime,
    }


def _db_binary_record(data: bytes, mime: str = "image/png") -> dict:
    return {
        "file_name": "chart.png",
        "content_text": None,
        "content_binary": data,
        "is_binary": True,
        "mime_type": mime,
    }


def _running_sandbox(returns: bytes | None) -> MagicMock:
    sandbox = MagicMock()
    sandbox.validate_and_normalize_path.return_value = ("/home/workspace/results/x", None)
    sandbox.adownload_file_bytes = AsyncMock(return_value=returns)
    sandbox.virtualize_path.return_value = "/results/x"
    return sandbox


# --- MIME mapping ---------------------------------------------------------


def test_mime_mapping_common_web_types():
    assert _guess_content_type("results/report.html") == "text/html; charset=utf-8"
    assert _guess_content_type("app.js") == "text/javascript; charset=utf-8"
    assert _guess_content_type("style.css") == "text/css; charset=utf-8"
    assert _guess_content_type("data.json") == "application/json; charset=utf-8"
    assert _guess_content_type("icon.svg") == "image/svg+xml"
    assert _guess_content_type("chart.png") == "image/png"
    assert _guess_content_type("photo.jpg") == "image/jpeg"
    assert _guess_content_type("font.woff2") == "font/woff2"


def test_mime_mapping_unknown_extension_is_octet_stream():
    assert _guess_content_type("blob.zzz") == "application/octet-stream"


# --- Traversal rejection → uniform 404 ------------------------------------


def test_has_traversal_helper():
    assert _has_traversal("a/../b")
    assert _has_traversal("../secret")
    assert _has_traversal("results\\..\\secret")
    assert not _has_traversal("results/report.html")
    assert not _has_traversal("a..b/c.html")  # dotdot inside a segment is allowed


@pytest.mark.asyncio
async def test_traversal_returns_uniform_404():
    # Traversal is rejected before any workspace lookup happens.
    with patch(_DBWS_PATCH, new=AsyncMock()) as mock_ws:
        with pytest.raises(HTTPException) as exc:
            await serve_workspace_file(WS_ID, "results/../../etc/passwd", inject_theme=False)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not found"
    mock_ws.assert_not_called()


# --- Unknown / flash workspace → uniform 404 ------------------------------


@pytest.mark.asyncio
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_unknown_workspace_returns_uniform_404(mock_ws, _wd):
    mock_ws.return_value = None
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file(WS_ID, "results/report.html", inject_theme=False)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not found"


@pytest.mark.asyncio
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_db_lookup_error_returns_uniform_404(mock_ws, _wd):
    mock_ws.side_effect = RuntimeError("db down")
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file(WS_ID, "results/report.html", inject_theme=False)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not found"


@pytest.mark.asyncio
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_flash_workspace_returns_uniform_404(mock_ws, _wd):
    mock_ws.return_value = _workspace("flash")
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file(WS_ID, "results/report.html", inject_theme=False)
    assert exc.value.status_code == 404


# --- DB fallback (stopped workspace) --------------------------------------


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_db_fallback_serves_text(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(
        return_value=_db_text_record("<html><head></head><body>hi</body></html>")
    )
    resp = await serve_workspace_file(WS_ID, "results/report.html", inject_theme=False)
    assert resp.status_code == 200
    assert resp.media_type == "text/html; charset=utf-8"
    assert b"<body>hi</body>" in resp.body
    mock_fp.get_file_content.assert_awaited_once_with(WS_ID, "results/report.html")


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_db_fallback_serves_binary(mock_ws, mock_fp, _wd, _vault):
    png = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03binarydata"
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(return_value=_db_binary_record(png))
    resp = await serve_workspace_file(WS_ID, "results/chart.png", inject_theme=False)
    assert resp.status_code == 200
    assert resp.media_type == "image/png"
    assert resp.body == png  # exact bytes, no redaction on binary


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_db_fallback_missing_file_returns_404(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file(WS_ID, "results/missing.html", inject_theme=False)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_db_fallback_unknown_extension_uses_db_mime(mock_ws, mock_fp, _wd, _vault):
    record = {
        "file_name": "data.bin",
        "content_text": None,
        "content_binary": b"raw",
        "is_binary": True,
        "mime_type": "application/x-custom",
    }
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(return_value=record)
    resp = await serve_workspace_file(WS_ID, "results/data.zzz", inject_theme=False)
    assert resp.media_type == "application/x-custom"


# --- Running sandbox source -----------------------------------------------


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_SANDBOX_PATCH, new_callable=AsyncMock)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_running_workspace_serves_live_bytes(mock_ws, mock_sb, _wd, _vault):
    mock_ws.return_value = _workspace("running")
    mock_sb.return_value = _running_sandbox(b"<html><body>live</body></html>")
    resp = await serve_workspace_file(WS_ID, "results/x.html", inject_theme=False)
    assert resp.status_code == 200
    assert b"live" in resp.body


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_SANDBOX_PATCH, new_callable=AsyncMock)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_running_workspace_missing_file_returns_404(mock_ws, mock_sb, _wd, _vault):
    mock_ws.return_value = _workspace("running")
    mock_sb.return_value = _running_sandbox(None)
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file(WS_ID, "results/x.html", inject_theme=False)
    assert exc.value.status_code == 404


# --- CSP + cache headers present on every response ------------------------


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_csp_header_present_on_html(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(return_value=_db_text_record("<html></html>"))
    resp = await serve_workspace_file(WS_ID, "results/report.html", inject_theme=False)
    assert resp.headers["Content-Security-Policy"] == "sandbox allow-scripts"
    assert resp.headers["Cache-Control"] == "private, max-age=60"
    assert resp.headers["Content-Disposition"].startswith("inline")
    assert "attachment" not in resp.headers["Content-Disposition"]


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_csp_header_present_on_binary(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(return_value=_db_binary_record(b"\x89PNGbytes"))
    resp = await serve_workspace_file(WS_ID, "results/chart.png", inject_theme=False)
    # CSP is on EVERY response, not just HTML.
    assert resp.headers["Content-Security-Policy"] == "sandbox allow-scripts"
    assert resp.headers["Cache-Control"] == "private, max-age=60"


# --- Vault-secret redaction for text content ------------------------------


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock)
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_redaction_applied_to_text(mock_ws, mock_fp, _wd, mock_vault):
    secret = "SUPERSECRETVALUE123"
    mock_vault.return_value = {"API_KEY": secret}
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(
        return_value=_db_text_record(f"<html><body>key={secret}</body></html>")
    )
    resp = await serve_workspace_file(WS_ID, "results/report.html", inject_theme=False)
    assert secret.encode() not in resp.body
    assert b"[REDACTED:API_KEY]" in resp.body


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock)
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_redaction_not_applied_to_binary(mock_ws, mock_fp, _wd, mock_vault):
    # A binary PNG that happens to contain the secret byte-sequence is served
    # verbatim — redaction only runs on text content types.
    secret = "SUPERSECRETVALUE123"
    mock_vault.return_value = {"API_KEY": secret}
    raw = b"\x89PNG" + secret.encode() + b"\x00\x01"
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(return_value=_db_binary_record(raw))
    resp = await serve_workspace_file(WS_ID, "results/chart.png", inject_theme=False)
    assert resp.body == raw


# --- ?inject=theme splices for HTML only ----------------------------------


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_inject_theme_splices_for_html(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    html = "<html><head><title>x</title></head><body>hi</body></html>"
    mock_fp.get_file_content = AsyncMock(return_value=_db_text_record(html))
    resp = await serve_workspace_file(WS_ID, "results/report.html", inject_theme=True)
    body = resp.body.decode()
    assert "widget:themeUpdate" in body
    assert 'content="light dark"' in body
    # Script spliced right after <head>, before the original <title>.
    assert body.index("widget:themeUpdate") < body.index("<title>")


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_inject_theme_not_applied_to_non_html(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    css = "body{color:red}"
    mock_fp.get_file_content = AsyncMock(
        return_value=_db_text_record(css, mime="text/css")
    )
    resp = await serve_workspace_file(WS_ID, "results/style.css", inject_theme=True)
    assert resp.body == css.encode()
    assert b"widget:themeUpdate" not in resp.body


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_plain_get_is_byte_faithful(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    html = "<html><head></head><body>exact bytes</body></html>"
    mock_fp.get_file_content = AsyncMock(return_value=_db_text_record(html))
    resp = await serve_workspace_file(WS_ID, "results/report.html", inject_theme=False)
    # No inject param → original bytes, no theme script.
    assert resp.body == html.encode()
    assert b"widget:themeUpdate" not in resp.body


# --- Endpoint wrapper: ?inject query param wiring -------------------------


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_endpoint_inject_theme_query_enables_splice(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    html = "<html><head></head><body>x</body></html>"
    mock_fp.get_file_content = AsyncMock(return_value=_db_text_record(html))
    resp = await serve_workspace_file_endpoint(
        workspace_id=WS_ID, path="results/report.html", inject="theme"
    )
    assert b"widget:themeUpdate" in resp.body


@pytest.mark.asyncio
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_endpoint_without_inject_is_byte_faithful(mock_ws, mock_fp, _wd, _vault):
    mock_ws.return_value = _workspace("stopped")
    html = "<html><head></head><body>x</body></html>"
    mock_fp.get_file_content = AsyncMock(return_value=_db_text_record(html))
    resp = await serve_workspace_file_endpoint(
        workspace_id=WS_ID, path="results/report.html", inject=None
    )
    assert resp.body == html.encode()


# --- ?format=pdf: render HTML to PDF --------------------------------------
#
# render_workspace_pdf is mocked everywhere — CI has no Chromium. The pre-
# validation path (resolve bytes + require HTML) reuses the DB-fallback fixtures.

_EXPECTED_INTERNAL_URL = f"{_PDF_INTERNAL_BASE}/api/v1/wsfiles/{WS_ID}/results/report.html"
_EXPECTED_SERVE_PREFIX = f"{_PDF_INTERNAL_BASE}/api/v1/wsfiles/{WS_ID}/"


@pytest.mark.asyncio
@patch(_RENDER_PATCH, new_callable=AsyncMock)
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_format_pdf_renders_html(mock_ws, mock_fp, _wd, _vault, mock_render):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(
        return_value=_db_text_record("<html><body>report</body></html>")
    )
    mock_render.return_value = b"%PDF-1.7 fake pdf bytes"

    resp = await serve_workspace_file_endpoint(
        workspace_id=WS_ID,
        path="results/report.html",
        inject=None,
        format="pdf",
        scale=None,
        page_numbers=False,
    )

    assert resp.status_code == 200
    assert resp.media_type == "application/pdf"
    assert resp.body == b"%PDF-1.7 fake pdf bytes"
    cd = resp.headers["Content-Disposition"]
    assert cd.startswith("attachment")
    assert 'filename="report.pdf"' in cd
    assert resp.headers["Cache-Control"] == "private, max-age=60"
    # No CSP on the PDF response.
    assert "Content-Security-Policy" not in resp.headers
    mock_render.assert_awaited_once_with(
        _EXPECTED_INTERNAL_URL,
        workspace_serve_prefix=_EXPECTED_SERVE_PREFIX,
        scale=None,
        page_numbers=False,
    )


@pytest.mark.asyncio
@patch(_RENDER_PATCH, new_callable=AsyncMock)
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_format_pdf_scale_and_page_numbers_pass_through(
    mock_ws, mock_fp, _wd, _vault, mock_render
):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(
        return_value=_db_text_record("<html><body>report</body></html>")
    )
    mock_render.return_value = b"%PDF-1.7 fake pdf bytes"

    resp = await serve_workspace_file_endpoint(
        workspace_id=WS_ID,
        path="results/report.html",
        inject=None,
        format="pdf",
        scale=0.8,
        page_numbers=True,
    )

    assert resp.status_code == 200
    mock_render.assert_awaited_once_with(
        _EXPECTED_INTERNAL_URL,
        workspace_serve_prefix=_EXPECTED_SERVE_PREFIX,
        scale=0.8,
        page_numbers=True,
    )


@pytest.mark.asyncio
@patch(_RENDER_PATCH, new_callable=AsyncMock)
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_format_pdf_on_non_html_returns_404_no_render(
    mock_ws, mock_fp, _wd, _vault, mock_render
):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(
        return_value=_db_text_record("body{color:red}", mime="text/css")
    )
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file_endpoint(
            workspace_id=WS_ID, path="results/style.css", inject=None, format="pdf"
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not found"
    mock_render.assert_not_awaited()


@pytest.mark.asyncio
@patch(_RENDER_PATCH, new_callable=AsyncMock)
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_format_pdf_on_missing_file_returns_404_no_render(
    mock_ws, mock_fp, _wd, _vault, mock_render
):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file_endpoint(
            workspace_id=WS_ID, path="results/missing.html", inject=None, format="pdf"
        )
    assert exc.value.status_code == 404
    mock_render.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "render_exc, expected_status",
    [
        (pdf_render.PdfRenderUnavailable("no chromium"), 501),
        (pdf_render.PdfRenderTimeout("timed out"), 504),
        (pdf_render.PdfRenderError("boom"), 500),
    ],
)
@patch(_RENDER_PATCH, new_callable=AsyncMock)
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_format_pdf_render_errors_map_to_status(
    mock_ws, mock_fp, _wd, _vault, mock_render, render_exc, expected_status
):
    mock_ws.return_value = _workspace("stopped")
    mock_fp.get_file_content = AsyncMock(
        return_value=_db_text_record("<html><body>x</body></html>")
    )
    mock_render.side_effect = render_exc
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file_endpoint(
            workspace_id=WS_ID, path="results/report.html", inject=None, format="pdf"
        )
    assert exc.value.status_code == expected_status


@pytest.mark.asyncio
@patch(_RENDER_PATCH, new_callable=AsyncMock)
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_format_pdf_traversal_returns_404_no_render(
    mock_ws, mock_fp, _wd, _vault, mock_render
):
    with pytest.raises(HTTPException) as exc:
        await serve_workspace_file_endpoint(
            workspace_id=WS_ID,
            path="results/../../etc/passwd",
            inject=None,
            format="pdf",
        )
    assert exc.value.status_code == 404
    mock_render.assert_not_awaited()


@pytest.mark.asyncio
@patch(_RENDER_PATCH, new_callable=AsyncMock)
@patch(_VAULT_PATCH, new_callable=AsyncMock, return_value={})
@patch(_WD_PATCH, return_value="/home/workspace")
@patch(_FP_PATCH)
@patch(_DBWS_PATCH, new_callable=AsyncMock)
async def test_unknown_format_serves_normally(mock_ws, mock_fp, _wd, _vault, mock_render):
    mock_ws.return_value = _workspace("stopped")
    html = "<html><head></head><body>plain</body></html>"
    mock_fp.get_file_content = AsyncMock(return_value=_db_text_record(html))
    # An unrecognized format value falls through to a normal inline serve.
    resp = await serve_workspace_file_endpoint(
        workspace_id=WS_ID, path="results/report.html", inject=None, format="docx"
    )
    assert resp.status_code == 200
    assert resp.media_type == "text/html; charset=utf-8"
    assert resp.body == html.encode()
    mock_render.assert_not_awaited()
