from __future__ import annotations

import json
import os
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .catalog import MODEL_DEFAULTS, PLANS, PROJECT_NAME
from .document_types import get_document_type, list_document_types
from .rbac import has_permission, permissions_for
from .security import create_session, hash_password, new_id, parse_session, verify_password
from .store import all_rows, audit, connect, create_personal_wallet, ensure_personal_workspace, execute, init_db, one

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
    user = one("SELECT id,email,full_name,status,session_version,created_at FROM users WHERE id=?", (payload.get("uid"),))
    if not user or user.get("status") != "active":
        return None
    if int(payload.get("sv", -1)) != int(user.get("session_version") or 0):
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


def _client_ip(request: Request) -> str:
    value = (request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if value:
        return value[:80]
    return str(request.client.host if request.client else "unknown")[:80]


def _login_blocked(email: str, ip: str) -> bool:
    email_limit = max(3, int(os.getenv("VBHC_LOGIN_EMAIL_LIMIT", "6")))
    ip_limit = max(email_limit, int(os.getenv("VBHC_LOGIN_IP_LIMIT", "30")))
    by_email = one("SELECT COUNT(*) n FROM auth_attempts WHERE success=0 AND email=? AND created_at>=datetime('now','-15 minutes')", (email,)) or {"n":0}
    by_ip = one("SELECT COUNT(*) n FROM auth_attempts WHERE success=0 AND ip=? AND created_at>=datetime('now','-15 minutes')", (ip,)) or {"n":0}
    return int(by_email.get("n") or 0) >= email_limit or int(by_ip.get("n") or 0) >= ip_limit


def _record_login_failure(email: str, ip: str) -> None:
    execute("INSERT INTO auth_attempts(email,ip,success) VALUES(?,?,0)", (email, ip))


def _clear_login_failures(email: str) -> None:
    execute("DELETE FROM auth_attempts WHERE email=? OR created_at < datetime('now','-2 days')", (email,))


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
    ip = _client_ip(request)
    if _login_blocked(email, ip):
        return _error("Đăng nhập tạm khóa do có quá nhiều lần thử. Vui lòng thử lại sau 15 phút.", 429)
    row = one("SELECT * FROM users WHERE email=?", (email,))
    if not row or not verify_password(password, row["password_hash"]):
        _record_login_failure(email, ip)
        return _error("Email hoặc mật khẩu không đúng", 401)
    if row["status"] != "active":
        return _error(f"Tài khoản đang ở trạng thái {row['status']}", 403)
    _clear_login_failures(email)
    token = create_session({"uid": row["id"], "email": row["email"], "sv": int(row.get("session_version") or 0)})
    audit(organization_id=None,user_id=row["id"],action="user.login",entity_type="session",after={"ip":ip})
    return _json({"ok": True, "token": token, "expires_in": 86400, "user": {"id":row["id"],"email":row["email"],"full_name":row["full_name"]}})


async def change_password(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    current_password = str(body.get("current_password") or "")
    new_password = str(body.get("new_password") or "")
    row = one("SELECT password_hash,session_version FROM users WHERE id=?", (user["id"],))
    if not row or not verify_password(current_password, row["password_hash"]):
        return _error("Mật khẩu hiện tại không đúng", 401)
    try:
        new_hash = hash_password(new_password)
    except ValueError as exc:
        return _error(str(exc))
    if verify_password(new_password, row["password_hash"]):
        return _error("Mật khẩu mới phải khác mật khẩu hiện tại")
    new_sv = int(row.get("session_version") or 0) + 1
    execute("UPDATE users SET password_hash=?,session_version=? WHERE id=?", (new_hash,new_sv,user["id"]))
    token = create_session({"uid": user["id"], "email": user["email"], "sv": new_sv})
    audit(organization_id=None,user_id=user["id"],action="user.password.change",entity_type="user",entity_id=user["id"])
    return _json({"ok": True, "token": token, "expires_in": 86400})


async def logout_all(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    new_sv = int(user.get("session_version") or 0) + 1
    execute("UPDATE users SET session_version=? WHERE id=?", (new_sv,user["id"]))
    audit(organization_id=None,user_id=user["id"],action="user.logout_all",entity_type="session")
    return _json({"ok": True})


async def me(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    ensure_personal_workspace(user["id"], user.get("full_name") or "", user.get("email") or "")
    memberships = all_rows("SELECT m.organization_id,o.name organization_name,o.workspace_type,m.department_id,m.role,m.status FROM memberships m JOIN organizations o ON o.id=m.organization_id WHERE m.user_id=? ORDER BY CASE o.workspace_type WHEN 'personal' THEN 0 ELSE 1 END,o.name", (user["id"],))
    wallet = one("SELECT * FROM wallets WHERE scope_type='user' AND scope_id=?", (user["id"],))
    sub = one("SELECT plan_id,status,COALESCE(starts_at,created_at) starts_at,ends_at FROM subscriptions WHERE scope_type='user' AND scope_id=? AND status='active' AND (ends_at IS NULL OR ends_at>CURRENT_TIMESTAMP) ORDER BY COALESCE(starts_at,created_at) DESC LIMIT 1", (user["id"],))
    return _json({"ok": True, "user": user, "platform_admin": platform_admin(user), "memberships": memberships, "wallet": wallet, "subscription": sub})


async def plans(_: Request):
    return _json({"ok": True, "plans": PLANS, "project": PROJECT_NAME})


async def activate_user(request: Request):
    admin = current_user(request)
    if not admin or not platform_admin(admin):
        return _error("Chỉ quản trị viên nền tảng được kích hoạt tài khoản", 403)
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
    policy = str(body.get("data_policy") or "internal").strip().lower()
    if policy not in {"public","internal","confidential","restricted"}:
        return _error("Chính sách dữ liệu không hợp lệ")
    org_id = new_id("org_")
    try:
        with connect() as db:
            db.execute("INSERT INTO organizations(id,name,slug,owner_user_id,data_policy,workspace_type) VALUES(?,?,?,?,?,?)", (org_id,name,slug,user["id"],policy,"organization"))
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
    dep_name = str(body.get("name") or "").strip()
    if len(dep_name) < 2:
        return _error("Tên phòng/khoa phải có ít nhất 2 ký tự")
    dep_id = new_id("dep_")
    execute("INSERT INTO departments(id,organization_id,parent_id,name,code,type,status) VALUES(?,?,?,?,?,?,?)", (dep_id,org_id,body.get("parent_id"),dep_name,body.get("code"),body.get("type") or "department","active"))
    audit(organization_id=org_id,user_id=user["id"],action="department.create",entity_type="department",entity_id=dep_id,after=body)
    return _json({"ok": True, "department_id": dep_id}, 201)


async def update_department(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    dep_id = request.path_params["dep_id"]
    if not user or not require_org_permission(user,org_id,"department.manage")[0]:
        return _error("Không có quyền quản lý phòng/khoa", 403)
    current = one("SELECT * FROM departments WHERE id=? AND organization_id=?", (dep_id,org_id))
    if not current:
        return _error("Không tìm thấy phòng/khoa", 404)
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    allowed = {"name","code","type","status","parent_id"}
    values = {k: body[k] for k in allowed if k in body}
    if "name" in values and len(str(values["name"] or "").strip()) < 2:
        return _error("Tên phòng/khoa phải có ít nhất 2 ký tự")
    if "status" in values and values["status"] not in {"active","suspended","archived"}:
        return _error("Trạng thái phòng/khoa không hợp lệ")
    if "type" in values and values["type"] not in {"department","clinical_department","unit"}:
        return _error("Loại đơn vị không hợp lệ")
    if values.get("parent_id") == dep_id:
        return _error("Đơn vị không thể là cấp trên của chính nó")
    if values.get("parent_id") and not one("SELECT id FROM departments WHERE id=? AND organization_id=?", (values["parent_id"],org_id)):
        return _error("Đơn vị cấp trên không hợp lệ")
    if not values:
        return _error("Không có trường hợp lệ để cập nhật")
    execute("UPDATE departments SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=? AND organization_id=?", tuple(values.values()) + (dep_id,org_id))
    after = one("SELECT * FROM departments WHERE id=? AND organization_id=?", (dep_id,org_id))
    audit(organization_id=org_id,user_id=user["id"],action="department.update",entity_type="department",entity_id=dep_id,before=current,after=after)
    return _json({"ok": True, "department": after})


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
    if target.get("status") != "active":
        return _error("Tài khoản thành viên chưa được kích hoạt", 409)
    if one("SELECT id FROM memberships WHERE organization_id=? AND user_id=? AND status='active'", (org_id,target["id"])):
        return _error("Người dùng đã là thành viên của cơ quan", 409)
    role = str(body.get("role") or "member")
    if role == "platform_super_admin" or not permissions_for(role):
        return _error("Vai trò không hợp lệ")
    execute("INSERT INTO memberships(id,organization_id,user_id,department_id,role,status) VALUES(?,?,?,?,?,?)", (new_id("mem_"),org_id,target["id"],body.get("department_id"),role,"active"))
    audit(organization_id=org_id,user_id=user["id"],action="member.add",entity_type="membership",entity_id=target["id"],after={"role":role,"department_id":body.get("department_id")})
    return _json({"ok": True, "user_id": target["id"], "role": role}, 201)


async def list_documents(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"document.read")[0]:
        return _error("Không có quyền xem văn bản", 403)
    direction = str(request.query_params.get("direction") or "").strip()
    status = str(request.query_params.get("status") or "").strip()
    priority = str(request.query_params.get("priority") or "").strip()
    query = str(request.query_params.get("q") or "").strip().lower()[:200]
    try:
        limit = min(500, max(1, int(request.query_params.get("limit") or 200)))
    except ValueError:
        limit = 200
    clauses = ["organization_id=?"]
    params: list[Any] = [org_id]
    if direction:
        if direction not in {"incoming","outgoing","internal"}:
            return _error("direction không hợp lệ")
        clauses.append("direction=?"); params.append(direction)
    if status:
        clauses.append("status=?"); params.append(status)
    if priority:
        clauses.append("priority=?"); params.append(priority)
    if query:
        like = f"%{query}%"
        clauses.append("(lower(subject) LIKE ? OR lower(COALESCE(symbol,'')) LIKE ? OR lower(COALESCE(sender,'')) LIKE ? OR lower(COALESCE(recipient,'')) LIKE ? OR lower(COALESCE(register_number,'')) LIKE ? OR lower(COALESCE(source_number,'')) LIKE ?)")
        params.extend([like] * 6)
    rows = all_rows(f"SELECT * FROM documents WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?", tuple(params + [limit]))
    for row in rows:
        try:
            row["metadata"] = json.loads(row.get("metadata") or "{}")
        except Exception:
            row["metadata"] = {}
    return _json({"ok": True, "documents": rows, "count": len(rows)})


async def create_document(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or not require_org_permission(user,org_id,"document.create")[0]:
        return _error("Không có quyền tạo văn bản", 403)
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    direction = str(body.get("direction") or "incoming").strip()
    if direction not in {"incoming","outgoing","internal"}:
        return _error("direction không hợp lệ")
    type_id = str(body.get("document_type") or "gov_cong_van").strip()
    doc_type = get_document_type(type_id)
    if not doc_type or direction not in doc_type.direction:
        return _error("Loại văn bản không phù hợp với chiều văn bản")
    requested_standard = str(body.get("standard") or doc_type.standard).strip()
    if requested_standard != doc_type.standard:
        return _error("Chuẩn văn bản không khớp loại văn bản")
    subject = str(body.get("subject") or "").strip()
    if len(subject) < 2:
        return _error("Cần nhập trích yếu văn bản")
    priority = str(body.get("priority") or "normal").strip()
    confidentiality = str(body.get("confidentiality") or "internal").strip()
    if priority not in {"normal","urgent","very_urgent"}:
        return _error("Mức độ ưu tiên không hợp lệ")
    if confidentiality not in {"public","internal","confidential","restricted"}:
        return _error("Mức độ dữ liệu không hợp lệ")
    initial_status = "received" if direction == "incoming" else "draft"
    doc_id = new_id("doc_")
    metadata = json.dumps(body.get("metadata") if isinstance(body.get("metadata"), dict) else {}, ensure_ascii=False)
    intake_number = None
    register_year = None
    received_at = None
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if direction == "incoming":
            register_year = int(db.execute("SELECT CAST(strftime('%Y','now') AS INTEGER)").fetchone()[0])
            row = db.execute("SELECT COALESCE(MAX(intake_number),0)+1 n FROM documents WHERE organization_id=? AND direction='incoming' AND register_year=?", (org_id,register_year)).fetchone()
            intake_number = int(row["n"] if row else 1)
            received_at = db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        db.execute("""INSERT INTO documents(id,organization_id,department_id,direction,document_type,standard,register_number,source_number,symbol,sender,recipient,subject,status,priority,confidentiality,deadline,parent_document_id,owner_user_id,intake_number,register_year,received_at,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (doc_id,org_id,body.get("department_id"),direction,type_id,doc_type.standard,body.get("register_number"),body.get("source_number"),body.get("symbol"),body.get("sender"),body.get("recipient"),subject,initial_status,priority,confidentiality,body.get("deadline"),body.get("parent_document_id"),user["id"],intake_number,register_year,received_at,metadata))
        db.commit()
    audit(organization_id=org_id,user_id=user["id"],action="document.create",entity_type="document",entity_id=doc_id,after={**body,"intake_number":intake_number,"register_year":register_year})
    return _json({"ok": True, "document_id": doc_id, "intake_number": intake_number, "register_year": register_year, "received_at": received_at}, 201)


async def wallet(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    org_id = request.query_params.get("organization_id")
    if org_id:
        if not org_role(user["id"],org_id) and not platform_admin(user):
            return _error("Không có quyền", 403)
        workspace = one("SELECT workspace_type,owner_user_id FROM organizations WHERE id=?", (org_id,))
        if workspace and workspace.get("workspace_type") == "personal":
            if workspace.get("owner_user_id") != user["id"] and not platform_admin(user):
                return _error("Kho cá nhân không thuộc tài khoản này", 403)
            data = one("SELECT * FROM wallets WHERE scope_type='user' AND scope_id=?", (user["id"],))
        else:
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
        return _error("Chỉ quản trị viên nền tảng được quản lý mô hình AI", 403)
    model_id = request.path_params["model_id"]
    body = await request.json()
    current = one("SELECT * FROM ai_models WHERE id=?", (model_id,))
    if not current:
        return _error("Không tìm thấy mô hình AI", 404)
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
        Route("/api/v2/auth/change-password", change_password, methods=["POST"]),
        Route("/api/v2/auth/logout-all", logout_all, methods=["POST"]),
        Route("/api/v2/me", me, methods=["GET"]),
        Route("/api/v2/plans", plans, methods=["GET"]),
        Route("/api/v2/admin/users/activate", activate_user, methods=["POST"]),
        Route("/api/v2/organizations", create_organization, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/departments", list_departments, methods=["GET"]),
        Route("/api/v2/organizations/{org_id}/departments", create_department, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/departments/{dep_id}", update_department, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/members", add_member, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/documents", list_documents, methods=["GET"]),
        Route("/api/v2/organizations/{org_id}/documents", create_document, methods=["POST"]),
        Route("/api/v2/document-types", lambda request: _json({"ok":True,"types":list_document_types(standard=request.query_params.get("standard"),direction=request.query_params.get("direction"))}), methods=["GET"]),
        Route("/api/v2/wallet", wallet, methods=["GET"]),
        Route("/api/v2/models", model_catalog, methods=["GET"]),
        Route("/api/v2/admin/models/{model_id}", update_model, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/audit", audit_log, methods=["GET"]),
    ]
