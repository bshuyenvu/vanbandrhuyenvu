from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .api import current_user, org_role, platform_admin
from .storage import storage_summary


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def my_storage(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    org_id = str(request.query_params.get("organization_id") or "").strip()
    if not org_id:
        return _error("Thiếu không gian lưu trữ")
    if not platform_admin(user) and not org_role(user["id"], org_id):
        return _error("Bạn không có quyền xem dung lượng này", 403)
    try:
        summary = storage_summary(user_id=user["id"], organization_id=org_id)
    except PermissionError as exc:
        return _error(str(exc), 403)
    except ValueError as exc:
        return _error(str(exc), 404)
    return JSONResponse({"ok": True, "storage": summary})


def routes() -> list[Route]:
    return [Route("/api/v2/storage", my_storage, methods=["GET"])]
