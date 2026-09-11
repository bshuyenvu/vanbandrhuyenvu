from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .api import current_user, org_role, platform_admin
from .store import active_plan, one, wallet_for
from .usage import usage_summary

STATIC_USAGE = Path(__file__).resolve().parents[1] / "static" / "usage.html"


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def usage_page(_: Request) -> Response:
    if not STATIC_USAGE.is_file():
        return _error("Thiếu giao diện theo dõi sử dụng", 500)
    return FileResponse(str(STATIC_USAGE), media_type="text/html; charset=utf-8", headers={"Cache-Control":"no-store"})


async def my_usage(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)

    organization_id = str(request.query_params.get("organization_id") or "").strip() or None
    if organization_id and not platform_admin(user) and not org_role(user["id"], organization_id):
        return _error("Bạn không thuộc cơ quan/đơn vị này", 403)

    usage_org_id = organization_id
    if organization_id:
        workspace = one("SELECT workspace_type,owner_user_id FROM organizations WHERE id=?", (organization_id,))
        if workspace and workspace.get("workspace_type") == "personal":
            if workspace.get("owner_user_id") != user["id"]:
                return _error("Kho cá nhân không thuộc tài khoản này", 403)
            plan_id = active_plan("user", user["id"])
            wallet = wallet_for(user_id=user["id"])
            usage_org_id = None
        else:
            plan_id = active_plan("organization", organization_id)
            wallet = wallet_for(user_id=user["id"], organization_id=organization_id)
    else:
        plan_id = active_plan("user", user["id"])
        wallet = wallet_for(user_id=user["id"])

    return JSONResponse({
        "ok": True,
        "usage": usage_summary(
            user_id=user["id"],
            plan_id=plan_id,
            wallet=wallet,
            organization_id=usage_org_id,
        ),
    })


def routes() -> list[Route]:
    return [
        Route("/usage", usage_page, methods=["GET"]),
        Route("/api/v2/usage", my_usage, methods=["GET"]),
    ]
