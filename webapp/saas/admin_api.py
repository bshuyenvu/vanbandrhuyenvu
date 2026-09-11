from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .api import current_user, platform_admin, require_org_permission
from .catalog import PLANS
from .rbac import ROLE_PERMISSIONS
from .store import adjust_wallet, all_rows, audit, create_personal_wallet, one, execute, set_subscription


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(message: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": message}, status)


async def platform_users(request: Request):
    user = current_user(request)
    if not user or not platform_admin(user):
        return _error("Chỉ quản trị viên nền tảng được xem tài khoản", 403)
    status = request.query_params.get("status")
    if status:
        rows = all_rows("SELECT id,email,full_name,status,created_at FROM users WHERE status=? ORDER BY created_at DESC LIMIT 500", (status,))
    else:
        rows = all_rows("SELECT id,email,full_name,status,created_at FROM users ORDER BY created_at DESC LIMIT 500")
    return _json({"ok": True, "users": rows})


async def set_user_status(request: Request):
    admin = current_user(request)
    if not admin or not platform_admin(admin):
        return _error("Chỉ quản trị viên nền tảng được thay đổi tài khoản", 403)
    user_id = request.path_params["user_id"]
    body = await request.json()
    status = str(body.get("status") or "")
    if status not in {"pending","active","suspended","locked","archived"}:
        return _error("Trạng thái không hợp lệ")
    before = one("SELECT id,email,status FROM users WHERE id=?", (user_id,))
    if not before:
        return _error("Không tìm thấy tài khoản", 404)
    if user_id == admin["id"] and status != "active":
        return _error("Không thể tự khóa hoặc vô hiệu hóa tài khoản quản trị viên nền tảng đang sử dụng", 409)
    execute("UPDATE users SET status=? WHERE id=?", (status,user_id))
    if status == "active":
        create_personal_wallet(user_id, "free")
    audit(organization_id=None,user_id=admin["id"],action="user.status",entity_type="user",entity_id=user_id,before=before,after={"status":status})
    return _json({"ok": True, "user_id": user_id, "status": status})


async def org_members(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"member.manage")[0]:
        return _error("Không có quyền quản lý thành viên", 403)
    rows = all_rows("""SELECT m.id membership_id,m.user_id,u.email,u.full_name,u.status user_status,m.department_id,d.name department_name,m.role,m.status membership_status,m.created_at FROM memberships m JOIN users u ON u.id=m.user_id LEFT JOIN departments d ON d.id=m.department_id WHERE m.organization_id=? ORDER BY u.full_name,u.email""", (org_id,))
    return _json({"ok": True, "members": rows, "roles": sorted(r for r in ROLE_PERMISSIONS if r != "platform_super_admin")})


async def update_membership(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    membership_id = request.path_params["membership_id"]
    if not user or not require_org_permission(user,org_id,"member.manage")[0]:
        return _error("Không có quyền quản lý thành viên", 403)
    before = one("SELECT * FROM memberships WHERE id=? AND organization_id=?", (membership_id,org_id))
    if not before:
        return _error("Không tìm thấy thành viên", 404)
    body = await request.json()
    role = body.get("role", before["role"])
    status = body.get("status", before["status"])
    department_id = body.get("department_id", before["department_id"])
    if role not in ROLE_PERMISSIONS or role == "platform_super_admin":
        return _error("Vai trò không hợp lệ")
    if status not in {"active","suspended","archived"}:
        return _error("Trạng thái thành viên không hợp lệ")
    if department_id and not one("SELECT id FROM departments WHERE id=? AND organization_id=? AND status='active'", (department_id,org_id)):
        return _error("Phòng/khoa không thuộc cơ quan hoặc đã ngưng hoạt động")
    if before.get("role") == "organization_owner" and role != "organization_owner":
        owners = one("SELECT COUNT(*) n FROM memberships WHERE organization_id=? AND role='organization_owner' AND status='active'", (org_id,)) or {"n":0}
        if int(owners.get("n") or 0) <= 1:
            return _error("Cơ quan phải còn ít nhất một chủ sở hữu", 409)
    execute("UPDATE memberships SET role=?,status=?,department_id=? WHERE id=? AND organization_id=?", (role,status,department_id,membership_id,org_id))
    audit(organization_id=org_id,user_id=user["id"],action="membership.update",entity_type="membership",entity_id=membership_id,before=before,after={"role":role,"status":status,"department_id":department_id})
    return _json({"ok": True, "membership_id": membership_id})


async def update_org_policy(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"policy.manage")[0]:
        return _error("Không có quyền quản lý chính sách dữ liệu", 403)
    body = await request.json()
    policy = str(body.get("data_policy") or "")
    if policy not in {"public","internal","confidential","restricted"}:
        return _error("Chính sách dữ liệu không hợp lệ")
    before = one("SELECT data_policy FROM organizations WHERE id=?", (org_id,))
    execute("UPDATE organizations SET data_policy=? WHERE id=?", (policy,org_id))
    audit(organization_id=org_id,user_id=user["id"],action="policy.update",entity_type="organization",entity_id=org_id,before=before,after={"data_policy":policy})
    return _json({"ok": True, "data_policy": policy})


async def update_storage_quota(request: Request):
    admin = current_user(request)
    org_id = request.path_params["org_id"]
    if not admin or not platform_admin(admin):
        return _error("Chỉ quản trị viên nền tảng được đặt dung lượng cơ quan", 403)
    body = await request.json()
    value = body.get("storage_quota_bytes")
    if value in (None, ""):
        quota = None
    else:
        try:
            quota = int(value)
        except Exception:
            return _error("Dung lượng không hợp lệ")
        if quota < 100 * 1024 * 1024:
            return _error("Dung lượng tối thiểu là 100 MB")
    before = one("SELECT storage_quota_bytes,workspace_type FROM organizations WHERE id=?", (org_id,))
    if not before:
        return _error("Không tìm thấy cơ quan", 404)
    if before.get("workspace_type") == "personal":
        return _error("Kho cá nhân sử dụng dung lượng theo gói người dùng, không đặt hạn mức cơ quan", 409)
    execute("UPDATE organizations SET storage_quota_bytes=? WHERE id=?", (quota, org_id))
    audit(organization_id=org_id,user_id=admin["id"],action="storage.quota.update",entity_type="organization",entity_id=org_id,before=before,after={"storage_quota_bytes":quota})
    return _json({"ok": True, "storage_quota_bytes": quota})


async def change_subscription(request: Request):
    admin = current_user(request)
    if not admin or not platform_admin(admin):
        return _error("Chỉ quản trị viên nền tảng được đổi gói thủ công", 403)
    body = await request.json()
    scope_type = str(body.get("scope_type") or "user")
    scope_id = str(body.get("scope_id") or "")
    plan_id = str(body.get("plan_id") or "")
    if scope_type not in {"user","organization"} or plan_id not in PLANS or not scope_id:
        return _error("Dữ liệu gói không hợp lệ")
    result = set_subscription(scope_type=scope_type,scope_id=scope_id,plan_id=plan_id)
    audit(organization_id=scope_id if scope_type=="organization" else None,user_id=admin["id"],action="subscription.change",entity_type="subscription",entity_id=result["subscription_id"],after={"scope_type":scope_type,"scope_id":scope_id,"plan_id":plan_id})
    return _json({"ok": True, **result})


async def credit_adjustment(request: Request):
    admin = current_user(request)
    if not admin or not platform_admin(admin):
        return _error("Chỉ quản trị viên nền tảng được điều chỉnh tín dụng AI", 403)
    body = await request.json()
    wallet_id = str(body.get("wallet_id") or "")
    delta = int(body.get("delta") or 0)
    if not wallet_id or delta == 0:
        return _error("Thiếu wallet_id hoặc delta")
    try:
        result = adjust_wallet(wallet_id=wallet_id,delta=delta,event_type=str(body.get("event_type") or "admin_adjustment"),note=str(body.get("note") or ""))
    except ValueError as exc:
        return _error(str(exc))
    audit(organization_id=None,user_id=admin["id"],action="wallet.adjust",entity_type="wallet",entity_id=wallet_id,after={"delta":delta,**result})
    return _json({"ok": True, **result})


async def platform_usage(request: Request):
    user = current_user(request)
    if not user or not platform_admin(user):
        return _error("Chỉ quản trị viên nền tảng được xem mức sử dụng toàn hệ thống", 403)
    summary = one("""SELECT COUNT(*) requests,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(cached_tokens),0) cached_tokens,COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(input_tokens+output_tokens),0) total_tokens,COALESCE(SUM(provider_cost_usd),0) provider_cost_usd,COALESCE(SUM(charged_credits),0) charged_credits FROM ai_usage""") or {}
    by_model = all_rows("""SELECT model_id,COUNT(*) requests,SUM(input_tokens) input_tokens,SUM(cached_tokens) cached_tokens,SUM(output_tokens) output_tokens,SUM(input_tokens+output_tokens) total_tokens,SUM(provider_cost_usd) provider_cost_usd,SUM(charged_credits) charged_credits FROM ai_usage GROUP BY model_id ORDER BY provider_cost_usd DESC""")
    return _json({"ok": True, "summary": summary, "by_model": by_model})


async def org_usage(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or (not require_org_permission(user,org_id,"usage.view")[0] and not require_org_permission(user,org_id,"billing.manage")[0]):
        return _error("Không có quyền xem mức sử dụng", 403)
    summary = one("""SELECT COUNT(*) requests,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(provider_cost_usd),0) provider_cost_usd,COALESCE(SUM(charged_credits),0) charged_credits FROM ai_usage WHERE organization_id=?""", (org_id,)) or {}
    return _json({"ok": True, "summary": summary})


async def platform_stats(request: Request):
    user = current_user(request)
    if not user or not platform_admin(user):
        return _error("Chỉ quản trị viên nền tảng", 403)
    return _json({"ok": True, "stats": {
        "users": (one("SELECT COUNT(*) n FROM users") or {"n":0})["n"],
        "pending_users": (one("SELECT COUNT(*) n FROM users WHERE status='pending'") or {"n":0})["n"],
        "organizations": (one("SELECT COUNT(*) n FROM organizations WHERE COALESCE(workspace_type,'organization')='organization'") or {"n":0})["n"],
        "documents": (one("SELECT COUNT(*) n FROM documents") or {"n":0})["n"],
        "ai_requests": (one("SELECT COUNT(*) n FROM ai_usage") or {"n":0})["n"],
    }})


def routes() -> list[Route]:
    return [
        Route("/api/v2/admin/users", platform_users, methods=["GET"]),
        Route("/api/v2/admin/users/{user_id}/status", set_user_status, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/members", org_members, methods=["GET"]),
        Route("/api/v2/organizations/{org_id}/members/{membership_id}", update_membership, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/policy", update_org_policy, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/storage-quota", update_storage_quota, methods=["PATCH"]),
        Route("/api/v2/admin/subscriptions", change_subscription, methods=["POST"]),
        Route("/api/v2/admin/credits", credit_adjustment, methods=["POST"]),
        Route("/api/v2/admin/usage", platform_usage, methods=["GET"]),
        Route("/api/v2/organizations/{org_id}/usage", org_usage, methods=["GET"]),
        Route("/api/v2/admin/stats", platform_stats, methods=["GET"]),
    ]
