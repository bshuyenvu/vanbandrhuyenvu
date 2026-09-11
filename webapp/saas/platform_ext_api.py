from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .api import current_user, platform_admin
from .store import all_rows


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(message: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": message}, status)


def _require_admin(request: Request):
    user = current_user(request)
    if not user or not platform_admin(user):
        return None
    return user


async def accounts(request: Request):
    if not _require_admin(request):
        return _error("Chỉ Platform Admin", 403)
    rows = all_rows("""SELECT u.id,u.email,u.full_name,u.status,u.created_at,
        COALESCE(w.id,'') wallet_id,COALESCE(w.balance_credits,0) balance_credits,
        COALESCE(s.plan_id,'free') plan_id
        FROM users u
        LEFT JOIN wallets w ON w.scope_type='user' AND w.scope_id=u.id
        LEFT JOIN subscriptions s ON s.id=(SELECT s2.id FROM subscriptions s2 WHERE s2.scope_type='user' AND s2.scope_id=u.id AND s2.status='active' ORDER BY s2.created_at DESC LIMIT 1)
        ORDER BY u.created_at DESC LIMIT 500""")
    return _json({"ok": True, "accounts": rows})


async def organizations(request: Request):
    if not _require_admin(request):
        return _error("Chỉ Platform Admin", 403)
    rows = all_rows("""SELECT o.id,o.name,o.slug,o.status,o.data_policy,o.created_at,
        u.email owner_email,COALESCE(w.id,'') wallet_id,COALESCE(w.balance_credits,0) balance_credits,
        COALESCE(s.plan_id,'organization') plan_id,
        (SELECT COUNT(*) FROM memberships m WHERE m.organization_id=o.id AND m.status='active') member_count,
        (SELECT COUNT(*) FROM documents d WHERE d.organization_id=o.id) document_count
        FROM organizations o
        LEFT JOIN users u ON u.id=o.owner_user_id
        LEFT JOIN wallets w ON w.scope_type='organization' AND w.scope_id=o.id
        LEFT JOIN subscriptions s ON s.id=(SELECT s2.id FROM subscriptions s2 WHERE s2.scope_type='organization' AND s2.scope_id=o.id AND s2.status='active' ORDER BY s2.created_at DESC LIMIT 1)
        ORDER BY o.created_at DESC LIMIT 300""")
    return _json({"ok": True, "organizations": rows})


async def wallets(request: Request):
    if not _require_admin(request):
        return _error("Chỉ Platform Admin", 403)
    rows = all_rows("SELECT * FROM wallets ORDER BY scope_type,scope_id")
    return _json({"ok": True, "wallets": rows})


def routes() -> list[Route]:
    return [
        Route("/api/v2/admin/accounts", accounts, methods=["GET"]),
        Route("/api/v2/admin/organizations", organizations, methods=["GET"]),
        Route("/api/v2/admin/wallets", wallets, methods=["GET"]),
    ]
