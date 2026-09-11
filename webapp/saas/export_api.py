from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from docx_export import build_government_reply
from government_docx import build_government_named_document
from party_docx import build_party_reply
from .api import current_user, org_role, platform_admin
from .billing_api import routes as billing_routes
from .document_types import get_document_type
from .platform_ext_api import routes as platform_ext_routes
from .workflow_api import routes as workflow_routes

STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"
STATIC_ADMIN = STATIC_ROOT / "admin-control.html"
STATIC_BILLING = STATIC_ROOT / "billing.html"


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _authorized(request: Request, body: dict[str, Any]) -> tuple[bool, JSONResponse | None]:
    user = current_user(request)
    if not user and os.getenv("VBHC_ALLOW_ANON_AI", "false").lower() not in {"1", "true", "yes"}:
        return False, _error("Vui lòng đăng nhập", 401)
    org_id = str(body.get("organization_id") or "").strip()
    if user and org_id and not platform_admin(user) and not org_role(user["id"], org_id):
        return False, _error("Bạn không thuộc cơ quan/đơn vị này", 403)
    return True, None


async def _static(path: Path) -> Response:
    if not path.is_file():
        return _error("Thiếu giao diện", 500)
    return FileResponse(str(path), media_type="text/html; charset=utf-8")


async def admin_control(_: Request) -> Response:
    return await _static(STATIC_ADMIN)


async def billing_page(_: Request) -> Response:
    return await _static(STATIC_BILLING)


async def export_docx_v2(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    ok, error = _authorized(request, body)
    if not ok:
        return error  # type: ignore[return-value]

    type_id = str(body.get("document_type") or "gov_cong_van").strip()
    doc_type = get_document_type(type_id)
    if not doc_type:
        return _error("Loại văn bản chưa được đăng ký", 404)
    if not doc_type.enabled_export:
        return _error(
            f"{doc_type.name} đã có trong danh mục nhưng renderer Word chưa được kiểm định; hệ thống không xuất bằng template khác.",
            422,
        )

    try:
        if doc_type.renderer == "government_reply":
            raw = build_government_reply(body)
        elif doc_type.renderer == "government_named":
            raw = build_government_named_document(body)
        elif doc_type.renderer == "party_reply":
            raw = build_party_reply(body)
        else:
            return _error("Renderer chưa được hỗ trợ", 422)
    except Exception as exc:
        return _error(f"Không tạo được DOCX: {exc}", 500)

    filename = str(body.get("download_name") or f"{type_id}.docx")
    if not filename.lower().endswith(".docx"):
        filename += ".docx"
    safe_name = filename.encode("ascii", "ignore").decode() or "document.docx"
    return Response(
        raw,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


def routes() -> list[Route]:
    result = [
        Route("/admin-control", admin_control, methods=["GET"]),
        Route("/billing", billing_page, methods=["GET"]),
        Route("/api/v2/export/docx", export_docx_v2, methods=["POST"]),
    ]
    result.extend(workflow_routes())
    result.extend(platform_ext_routes())
    result.extend(billing_routes())
    return result
