from __future__ import annotations

import json
import os
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .catalog import MODEL_DEFAULTS, PLANS, PROJECT_NAME
from .document_types import list_document_types
from .rbac import has_permission, permissions_for
from .security import create_session, hash_password, new_id, parse_session, verify_password
from .store import all_rows, audit, connect, create_personal_wallet, execute, init_db, one

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(message: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": message}, status)


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def current_user(request: Request) -> dict[str, Any] | None:
    payload = parse_session(_bearer(request))
    if not payload:
        return None
    user = one("SELECT id,email,full_name,status,created_at FROM users WHERE id=?", (payload.get("uid"),))
    if not user or user.get("status") != "active":
        return None
    return user


def platform_admin(user: dict[str, Any]) -> bool:
    allow = {x.strip().lower() for x in os.getenv("VBHC_PLATFORM_ADMIN_EMAILS", "").split(",") if x.strip()}
    return user.get("email", "").lower() in allow


def org_role(user_id: str, organization_id: str) -> str | None:
    row = one("SELECT role FROM memberships WHERE organization_id=? AND user_id=? AND status='active' ORDER BY CASE role WHEN 'organization_owner' THEN 0 ELSE 1 END LIMIT 1", (organization_id, user_id))
    return row["role"] if row else None


def require_org_permission(user: dict[str, Any], organization_id: str, permission: str) -> tuple[bool, str | None]:
    if platform_admin(user):
        return True, "platform_super_admin"
    role = org_role(user["id"], organization_id)
    return bool(role and has_permission(role, permission)), role


async def register(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    full_name = str(body.get("full_name") or "").strip()
    if not EMAIL_RE.match(email):
        return _error("Email không hợp lệ")
    if one("SELECT id FROM users WHERE email=?", (email,)):
        return _error("Email đã được đăng ký", 409)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        return _error(str(exc))
    user_id = new_id("usr_")
    auto = os.getenv("VBHC_AUTO_ACTIVATE_FREE", "false").lower() in {"1","true","yes"}
    status = "active" if auto else "pending"
    execute("INSERT INTO users(id,email,password_hash,full_name,status) VALUES(?,?,?,?,?)", (user_id,email,password_hash,full_name,status))
    if auto:
        create_personal_wallet(user_id, "free")
    audit(organization_id=None,user_id=user_id,action="user.register",entity_type="user",entity_id=user_id,after={"email":email,"status":status})
    return _json({"ok": True, "user_id": user_id, "status": status, "activation_required": not auto, "plan": "free"}, 201)


async def login(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    row = one("SELECT * FROM users WHERE email=?", (email,))
    if not row or not verify_password(password, row["password_hash"]):
        return _error("Email hoặc mật khẩu không đúng", 401)
    if row["status"] != "active":
        return _error(f"Tài khoản đang ở trạng thái {row['status']}", 403)
    token = create_session({"uid": row["id"], "email": row["email"]})
    audit(organization_id=None,user_id=row["id"],action="user.login",entity_type="session")
    return _json({"ok": True, "token": token, "user": {"id":row["id"],"email":row["email"],"full_name":row["full_name"]}})


async def me(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    memberships = all_rows("SELECT m.organization_id,o.name organization_name,m.department_id,m.role,m.status FROM memberships m JOIN organizations o ON o.id=m.organization_id WHERE m.user_id=?", (user["id"],))
    wallet = one("SELECT * FROM wallets WHERE scope_type='user' AND scope_id=?", (user["id"],))
    sub = one("SELECT plan_id,status FROM subscriptions WHERE scope_type='user' AND scope_id=? ORDER BY created_at DESC LIMIT 1", (user["id"],))
    return _json({"ok": True, "user": user, "platform_admin": platform_admin(user), "memberships": memberships, "wallet": wallet, "subscription": sub})


async def plans(_: Request):
    return _json({"ok": True, "plans": PLANS, "project": PROJECT_NAME})


async def activate_user(request: Request):
    admin = current_user(request)
    if not admin or not platform_admin(admin):
        return _error("Chỉ Platform Admin được kích hoạt tài khoản", 403)
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    user_id = str(body.get("user_id") or "")
    target = one("SELECT id,status FROM users WHERE id=?", (user_id,))
    if not target:
        return _error("Không tìm thấy tài khoản", 404)
    execute("UPDATE users SET status='active' WHERE id=?", (user_id,))
    create_personal_wallet(user_id, "free")
    audit(organization_id=None,user_id=admin["id"],action="user.activate",entity_type="user",entity_id=user_id,before={"status":target["status"]},after={"status":"active"})
    return _json({"ok": True, "user_id": user_id, "status": "active"})


async def create_organization(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    name = str(body.get("name") or "").strip()
    slug = re.sub(r"[^a-z0-9-]+", "-", str(body.get("slug") or name).lower()).strip("-")
    if not name or not slug:
        return _error("Thiếu tên tổ chức")
    org_id = new_id("org_")
    try:
        with connect() as db:
            db.execute("INSERT INTO organizations(id,name,slug,owner_user_id,data_policy) VALUES(?,?,?,?,?)", (org_id,name,slug,user["id"],str(body.get("data_policy") or "internal")))
            db.execute("INSERT INTO memberships(id,organization_id,user_id,department_id,role,status) VALUES(?,?,?,?,?,?)", (new_id("mem_"),org_id,user["id"],None,"organization_owner","active"))
            wallet_id = new_id("wal_")
            db.execute("INSERT INTO wallets(id,scope_type,scope_id,balance_credits) VALUES(?,?,?,0)", (wallet_id,"organization",org_id))
            db.commit()
    except Exception as exc:
        return _error(f"Không tạo được tổ chức: {exc}", 409)
    audit(organization_id=org_id,user_id=user["id"],action="organization.create",entity_type="organization",entity_id=org_id,after={"name":name,"slug":slug})
    return _json({"ok": True, "organization": {"id":org_id,"name":name,"slug":slug}}, 201)


async def list_departments(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"document.read")[0]:
        return _error("Không có quyền", 403)
    return _json({"ok": True, "departments": all_rows("SELECT * FROM departments WHERE organization_id=? ORDER BY name", (org_id,))})


async def create_department(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    allowed, _ = require_org_permission(user or {},org_id,"department.manage") if user else (False,None)
    if not allowed:
        return _error("Không có quyền quản lý phòng/khoa", 403)
    body = await request.json()
    dep_id = new_id("dep_")
    execute("INSERT INTO departments(id,organization_id,parent_id,name,code,type,status) VALUES(?,?,?,?,?,?,?)", (dep_id,org_id,body.get("parent_id"),str(body.get("name") or "").strip(),body.get("code"),body.get("type") or "department","active"))
    audit(organization_id=org_id,user_id=user["id"],action="department.create",entity_type="department",entity_id=dep_id,after=body)
    return _json({"ok": True, "department_id": dep_id}, 201)


async def add_member(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"member.manage")[0]:
        return _error("Không có quyền quản lý thành viên", 403)
    body = await request.json()
    email = str(body.get("email") or "").lower().strip()
    target = one("SELECT id,status FROM users WHERE email=?", (email,))
    if not target:
        return _error("Người dùng cần đăng ký tài khoản trước", 404)
    role = str(body.get("role") or "member")
    if not permissions_for(role):
        return _error("Vai trò không hợp lệ")
    execute("INSERT INTO memberships(id,organization_id,user_id,department_id,role,status) VALUES(?,?,?,?,?,?)", (new_id("mem_"),org_id,target["id"],body.get("department_id"),role,"active"))
    audit(organization_id=org_id,user_id=user["id"],action="member.add",entity_type="membership",entity_id=target["id"],after={"role":role,"department_id":body.get("department_id")})
    return _json({"ok": True, "user_id": target["id"], "role": role}, 201)


async def list_documents(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"document.read")[0]:
        return _error("Không có quyền xem văn bản", 403)
    direction = request.query_params.get("direction")
    if direction:
        rows = all_rows("SELECT * FROM documents WHERE organization_id=? AND direction=? ORDER BY created_at DESC LIMIT 200", (org_id,direction))
    else:
        rows = all_rows("SELECT * FROM documents WHERE organization_id=? ORDER BY created_at DESC LIMIT 200", (org_id,))
    for row in rows:
        try: row["metadata"] = json.loads(row.get("metadata") or "{}")
        except Exception: pass
    return _json({"ok": True, "documents": rows})


async def create_document(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"document.create")[0]:
        return _error("Không có quyền tạo văn bản", 403)
    body = await request.json()
    direction = str(body.get("direction") or "incoming")
    if direction not in {"incoming","outgoing","internal"}:
        return _error("direction không hợp lệ")
    doc_id = new_id("doc_")
    metadata = json.dumps(body.get("metadata") or {}, ensure_ascii=False)
    execute("""INSERT INTO documents(id,organization_id,department_id,direction,document_type,standard,register_number,source_number,symbol,sender,recipient,subject,status,priority,confidentiality,deadline,parent_document_id,owner_user_id,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id,org_id,body.get("department_id"),direction,body.get("document_type") or "gov_cong_van",body.get("standard") or "government",body.get("register_number"),body.get("source_number"),body.get("symbol"),body.get("sender"),body.get("recipient"),body.get("subject") or "",body.get("status") or "draft",body.get("priority") or "normal",body.get("confidentiality") or "internal",body.get("deadline"),body.get("parent_document_id"),user["id"],metadata))
    audit(organization_id=org_id,user_id=user["id"],action="document.create",entity_type="document",entity_id=doc_id,after=body)
    return _json({"ok": True, "document_id": doc_id}, 201)


async def wallet(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    org_id = request.query_params.get("organization_id")
    if org_id:
        if not org_role(user["id"],org_id) and not platform_admin(user):
            return _error("Không có quyền", 403)
        data = one("SELECT * FROM wallets WHERE scope_type='organization' AND scope_id=?", (org_id,))
    else:
        data = one("SELECT * FROM wallets WHERE scope_type='user' AND scope_id=?", (user["id"],))
    return _json({"ok": True, "wallet": data})


async def model_catalog(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    rows = all_rows("SELECT id,provider,model_name,display_name,tier,enabled,input_usd_per_million,cached_input_usd_per_million,output_usd_per_million,service_multiplier,daily_budget_usd FROM ai_models ORDER BY tier,display_name")
    return _json({"ok": True, "models": rows})


async def update_model(request: Request):
    user = current_user(request)
    if not user or not platform_admin(user):
        return _error("Chỉ Platform Admin được quản lý model", 403)
    model_id = request.path_params["model_id"]
    body = await request.json()
    current = one("SELECT * FROM ai_models WHERE id=?", (model_id,))
    if not current:
        return _error("Không tìm thấy model", 404)
    fields = ["display_name","tier","enabled","input_usd_per_million","cached_input_usd_per_million","output_usd_per_million","service_multiplier","daily_budget_usd"]
    values = {k: body[k] for k in fields if k in body}
    if values:
        sets = ",".join(f"{k}=?" for k in values)
        execute(f"UPDATE ai_models SET {sets} WHERE id=?", tuple(values.values()) + (model_id,))
    audit(organization_id=None,user_id=user["id"],action="model.update",entity_type="ai_model",entity_id=model_id,before=current,after=values)
    return _json({"ok": True, "model": one("SELECT * FROM ai_models WHERE id=?", (model_id,))})


async def audit_log(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"audit.view")[0]:
        return _error("Không có quyền xem nhật ký", 403)
    return _json({"ok": True, "events": all_rows("SELECT * FROM audit_logs WHERE organization_id=? ORDER BY id DESC LIMIT 300", (org_id,))})


def routes() -> list[Route]:
    init_db()
    return [
        Route("/api/v2/auth/register", register, methods=["POST"]),
        Route("/api/v2/auth/login", login, methods=["POST"]),
        Route("/api/v2/me", me, methods=["GET"]),
        Route("/api/v2/plans", plans, methods=["GET"]),
        Route("/api/v2/admin/users/activate", activate_user, methods=["POST"]),
        Route("/api/v2/organizations", create_organization, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/departments", list_departments, methods=["GET"]),
        Route("/api/v2/organizations/{org_id}/departments", create_department, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/members", add_member, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/documents", list_documents, methods=["GET"]),
        Route("/api/v2/organizations/{org_id}/documents", create_document, methods=["POST"]),
        Route("/api/v2/document-types", lambda request: _json({"ok":True,"types":list_document_types(standard=request.query_params.get("standard"),direction=request.query_params.get("direction"))}), methods=["GET"]),
        Route("/api/v2/wallet", wallet, methods=["GET"]),
        Route("/api/v2/models", model_catalog, methods=["GET"]),
        Route("/api/v2/admin/models/{model_id}", update_model, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/audit", audit_log, methods=["GET"]),
    ]
