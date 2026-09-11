from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .api import current_user, require_org_permission
from .security import new_id
from .store import all_rows, audit, connect, execute, one

VALID_TRANSITIONS = {
    "draft": {"submitted", "cancelled"},
    "received": {"assigned", "processing", "closed"},
    "assigned": {"processing", "closed"},
    "processing": {"submitted", "closed"},
    "submitted": {"approved", "rejected", "processing"},
    "rejected": {"processing", "submitted", "cancelled"},
    "approved": {"issued", "archived"},
    "issued": {"archived"},
    "closed": {"archived"},
    "archived": set(),
    "cancelled": set(),
}


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(message: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": message}, status)


def init_workflow_schema() -> None:
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS document_assignments(
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL, organization_id TEXT NOT NULL,
            department_id TEXT, assignee_user_id TEXT, assignment_role TEXT NOT NULL DEFAULT 'primary',
            status TEXT NOT NULL DEFAULT 'assigned', due_at TEXT, assigned_by TEXT,
            note TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_assign_doc ON document_assignments(document_id,created_at)")
        db.commit()


async def document_detail(request: Request):
    user = current_user(request)
    org_id, doc_id = request.path_params["org_id"], request.path_params["doc_id"]
    if not user or not require_org_permission(user, org_id, "document.read")[0]:
        return _error("Không có quyền xem văn bản", 403)
    doc = one("SELECT * FROM documents WHERE id=? AND organization_id=?", (doc_id, org_id))
    if not doc:
        return _error("Không tìm thấy văn bản", 404)
    assignments = all_rows("SELECT * FROM document_assignments WHERE document_id=? ORDER BY created_at DESC", (doc_id,))
    versions = all_rows("SELECT * FROM document_versions WHERE document_id=? ORDER BY version DESC", (doc_id,))
    return _json({"ok": True, "document": doc, "assignments": assignments, "versions": versions})


async def assign_document(request: Request):
    user = current_user(request)
    org_id, doc_id = request.path_params["org_id"], request.path_params["doc_id"]
    if not user or not require_org_permission(user, org_id, "document.assign")[0]:
        return _error("Không có quyền giao xử lý văn bản", 403)
    doc = one("SELECT id,status FROM documents WHERE id=? AND organization_id=?", (doc_id, org_id))
    if not doc:
        return _error("Không tìm thấy văn bản", 404)
    body = await request.json()
    assignee = str(body.get("assignee_user_id") or "").strip() or None
    department = str(body.get("department_id") or "").strip() or None
    if not assignee and not department:
        return _error("Cần chọn người xử lý hoặc phòng/khoa")
    if assignee:
        member = one("SELECT id FROM memberships WHERE organization_id=? AND user_id=? AND status='active'", (org_id, assignee))
        if not member:
            return _error("Người được giao không phải thành viên active của cơ quan")
    if department:
        dep = one("SELECT id FROM departments WHERE organization_id=? AND id=? AND status='active'", (org_id, department))
        if not dep:
            return _error("Phòng/khoa không hợp lệ")
    assignment_id = new_id("asg_")
    execute("""INSERT INTO document_assignments(id,document_id,organization_id,department_id,assignee_user_id,assignment_role,status,due_at,assigned_by,note)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (assignment_id,doc_id,org_id,department,assignee,body.get("role") or "primary","assigned",body.get("due_at"),user["id"],body.get("note")))
    if doc["status"] in {"received", "draft"}:
        execute("UPDATE documents SET status='assigned',updated_at=CURRENT_TIMESTAMP WHERE id=?", (doc_id,))
    audit(organization_id=org_id,user_id=user["id"],action="document.assign",entity_type="document",entity_id=doc_id,after={"assignment_id":assignment_id,"assignee_user_id":assignee,"department_id":department,"due_at":body.get("due_at")})
    return _json({"ok": True, "assignment_id": assignment_id}, 201)


async def transition_document(request: Request):
    user = current_user(request)
    org_id, doc_id = request.path_params["org_id"], request.path_params["doc_id"]
    if not user:
        return _error("Chưa đăng nhập", 401)
    doc = one("SELECT * FROM documents WHERE id=? AND organization_id=?", (doc_id, org_id))
    if not doc:
        return _error("Không tìm thấy văn bản", 404)
    body = await request.json()
    target = str(body.get("status") or "").strip()
    current = str(doc.get("status") or "draft")
    if target not in VALID_TRANSITIONS.get(current, set()):
        return _error(f"Không thể chuyển trạng thái {current} → {target}", 409)

    permission = "document.approve" if target in {"approved", "rejected"} else "document.issue" if target == "issued" else "document.create"
    if not require_org_permission(user, org_id, permission)[0]:
        return _error(f"Không có quyền thực hiện trạng thái {target}", 403)
    execute("UPDATE documents SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (target, doc_id))
    audit(organization_id=org_id,user_id=user["id"],action=f"document.{target}",entity_type="document",entity_id=doc_id,before={"status":current},after={"status":target,"note":body.get("note")})
    return _json({"ok": True, "document_id": doc_id, "status": target})


async def add_version(request: Request):
    user = current_user(request)
    org_id, doc_id = request.path_params["org_id"], request.path_params["doc_id"]
    if not user or not require_org_permission(user, org_id, "document.version")[0]:
        return _error("Không có quyền tạo phiên bản văn bản", 403)
    if not one("SELECT id FROM documents WHERE id=? AND organization_id=?", (doc_id, org_id)):
        return _error("Không tìm thấy văn bản", 404)
    body = await request.json()
    last = one("SELECT MAX(version) max_version FROM document_versions WHERE document_id=?", (doc_id,)) or {"max_version": 0}
    version = int(last.get("max_version") or 0) + 1
    version_id = new_id("ver_")
    execute("INSERT INTO document_versions(id,document_id,version,content_text,created_by,ai_model_id,change_note) VALUES(?,?,?,?,?,?,?)",
            (version_id,doc_id,version,str(body.get("content_text") or ""),user["id"],body.get("ai_model_id"),body.get("change_note")))
    audit(organization_id=org_id,user_id=user["id"],action="document.version.create",entity_type="document",entity_id=doc_id,after={"version":version,"version_id":version_id})
    return _json({"ok": True, "version_id": version_id, "version": version}, 201)


def routes() -> list[Route]:
    init_workflow_schema()
    return [
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}", document_detail, methods=["GET"]),
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}/assign", assign_document, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}/status", transition_document, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}/versions", add_version, methods=["POST"]),
    ]
