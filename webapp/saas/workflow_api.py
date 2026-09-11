from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .api import current_user, require_org_permission
from .security import new_id
from .storage import ensure_storage_capacity
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


ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".webp"}
ATTACHMENT_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}


def _attachment_root() -> Path:
    default = Path(__file__).resolve().parents[2] / "data" / "files"
    root = Path(os.getenv("VBHC_FILE_ROOT", str(default))).expanduser().resolve()
    required_mount_raw = os.getenv("VBHC_REQUIRED_STORAGE_MOUNT", "").strip()
    if required_mount_raw:
        required_mount = Path(required_mount_raw).expanduser().resolve()
        if not required_mount.is_mount():
            raise RuntimeError(f"Kho lưu trữ bắt buộc chưa được mount: {required_mount}")
        if root != required_mount and required_mount not in root.parents:
            raise RuntimeError("VBHC_FILE_ROOT nằm ngoài storage mount bắt buộc")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _safe_original_name(value: str) -> str:
    name = Path(value or "document").name.strip()[:180]
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    return name or "document"


def _valid_attachment_content(ext: str, raw: bytes) -> bool:
    if ext == ".pdf":
        return raw.startswith(b"%PDF-")
    if ext == ".png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in {".jpg", ".jpeg"}:
        return raw.startswith(b"\xff\xd8\xff")
    if ext == ".webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if ext == ".txt":
        try:
            raw.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    if ext == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = set(zf.namelist())
                return "[Content_Types].xml" in names and "word/document.xml" in names
        except Exception:
            return False
    return False


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
    try:
        doc["metadata"] = json.loads(doc.get("metadata") or "{}")
    except Exception:
        doc["metadata"] = {}
    assignments = all_rows("""SELECT a.*,d.name department_name,u.full_name assignee_name,u.email assignee_email,
        au.full_name assigned_by_name FROM document_assignments a
        LEFT JOIN departments d ON d.id=a.department_id
        LEFT JOIN users u ON u.id=a.assignee_user_id
        LEFT JOIN users au ON au.id=a.assigned_by
        WHERE a.document_id=? ORDER BY a.created_at DESC""", (doc_id,))
    versions = all_rows("""SELECT v.*,u.full_name created_by_name FROM document_versions v
        LEFT JOIN users u ON u.id=v.created_by WHERE v.document_id=? ORDER BY v.version DESC""", (doc_id,))
    attachments = all_rows("""SELECT a.id,a.original_name,a.mime_type,a.size_bytes,a.sha256,a.created_at,a.uploaded_by,
        u.full_name uploaded_by_name FROM document_attachments a LEFT JOIN users u ON u.id=a.uploaded_by
        WHERE a.document_id=? AND a.organization_id=? ORDER BY a.created_at DESC""", (doc_id,org_id))
    events = all_rows("SELECT id,action,user_id,before_data,after_data,created_at FROM audit_logs WHERE organization_id=? AND entity_type='document' AND entity_id=? ORDER BY id DESC LIMIT 100", (org_id,doc_id))
    return _json({"ok": True, "document": doc, "assignments": assignments, "versions": versions, "attachments": attachments, "events": events, "transitions": sorted(VALID_TRANSITIONS.get(str(doc.get("status") or "draft"), set()))})


async def update_document(request: Request):
    user = current_user(request)
    org_id, doc_id = request.path_params["org_id"], request.path_params["doc_id"]
    if not user or not require_org_permission(user, org_id, "document.create")[0]:
        return _error("Không có quyền cập nhật văn bản", 403)
    current = one("SELECT * FROM documents WHERE id=? AND organization_id=?", (doc_id, org_id))
    if not current:
        return _error("Không tìm thấy văn bản", 404)
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    allowed = {"department_id","register_number","source_number","symbol","sender","recipient","subject","priority","confidentiality","deadline","metadata"}
    values = {k: body[k] for k in allowed if k in body}
    if "subject" in values and not str(values["subject"] or "").strip():
        return _error("Trích yếu không được để trống")
    if "priority" in values and values["priority"] not in {"normal","urgent","very_urgent"}:
        return _error("Mức độ ưu tiên không hợp lệ")
    if "confidentiality" in values and values["confidentiality"] not in {"public","internal","confidential","restricted"}:
        return _error("Mức độ dữ liệu không hợp lệ")
    if values.get("department_id"):
        if not one("SELECT id FROM departments WHERE id=? AND organization_id=? AND status='active'", (values["department_id"],org_id)):
            return _error("Phòng/khoa không hợp lệ")
    if "metadata" in values:
        if not isinstance(values["metadata"], dict):
            return _error("metadata phải là object")
        values["metadata"] = json.dumps(values["metadata"], ensure_ascii=False)
    if not values:
        return _error("Không có trường hợp lệ để cập nhật")
    sets = ",".join(f"{k}=?" for k in values) + ",updated_at=CURRENT_TIMESTAMP"
    execute(f"UPDATE documents SET {sets} WHERE id=? AND organization_id=?", tuple(values.values()) + (doc_id,org_id))
    after = one("SELECT * FROM documents WHERE id=? AND organization_id=?", (doc_id,org_id))
    audit(organization_id=org_id,user_id=user["id"],action="document.update",entity_type="document",entity_id=doc_id,before=current,after=after)
    return _json({"ok": True, "document": after})


async def upload_attachment(request: Request):
    user = current_user(request)
    org_id, doc_id = request.path_params["org_id"], request.path_params["doc_id"]
    if not user or not require_org_permission(user, org_id, "document.version")[0]:
        return _error("Không có quyền đính kèm tệp", 403)
    if not one("SELECT id FROM documents WHERE id=? AND organization_id=?", (doc_id,org_id)):
        return _error("Không tìm thấy văn bản", 404)
    try:
        body = await request.json()
    except Exception:
        return _error("Body phải là JSON")
    original = _safe_original_name(str(body.get("filename") or "document"))
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        return _error("Chỉ cho phép PDF, DOCX, TXT, PNG, JPG/JPEG hoặc WEBP")
    encoded = str(body.get("file_base64") or "")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        return _error("Dữ liệu tệp mã hóa không hợp lệ")
    max_bytes = max(1024 * 1024, int(os.getenv("VBHC_ATTACHMENT_MAX_BYTES", str(12 * 1024 * 1024))))
    if not raw:
        return _error("Tệp rỗng")
    if len(raw) > max_bytes:
        return _error(f"Tệp vượt quá {max_bytes // (1024 * 1024)} MB", 413)
    if not _valid_attachment_content(ext, raw):
        return _error("Nội dung tệp không khớp định dạng hoặc tệp bị hỏng", 422)
    try:
        ensure_storage_capacity(user_id=user["id"], organization_id=org_id, incoming_bytes=len(raw))
    except PermissionError as exc:
        return _error(str(exc), 403)
    except ValueError as exc:
        return _error(str(exc), 413)
    attachment_id = new_id("att_")
    root = _attachment_root()
    target_dir = (root / org_id / doc_id).resolve()
    if root not in target_dir.parents:
        return _error("Đường dẫn lưu tệp không hợp lệ", 500)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stored_name = attachment_id + ext
    target = target_dir / stored_name
    target.write_bytes(raw)
    target.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    mime = ATTACHMENT_MIME[ext]
    try:
        execute("INSERT INTO document_attachments(id,document_id,organization_id,original_name,stored_name,mime_type,size_bytes,sha256,uploaded_by) VALUES(?,?,?,?,?,?,?,?,?)",
                (attachment_id,doc_id,org_id,original,stored_name,mime,len(raw),digest,user["id"]))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    audit(organization_id=org_id,user_id=user["id"],action="document.attachment.upload",entity_type="document",entity_id=doc_id,after={"attachment_id":attachment_id,"filename":original,"size_bytes":len(raw),"sha256":digest})
    return _json({"ok": True, "attachment": {"id":attachment_id,"filename":original,"mime_type":mime,"size_bytes":len(raw),"sha256":digest}}, 201)


async def download_attachment(request: Request):
    user = current_user(request)
    org_id, doc_id, attachment_id = request.path_params["org_id"], request.path_params["doc_id"], request.path_params["attachment_id"]
    if not user or not require_org_permission(user, org_id, "document.read")[0]:
        return _error("Không có quyền tải tệp", 403)
    item = one("SELECT * FROM document_attachments WHERE id=? AND document_id=? AND organization_id=?", (attachment_id,doc_id,org_id))
    if not item:
        return _error("Không tìm thấy tệp", 404)
    root = _attachment_root()
    target = (root / org_id / doc_id / item["stored_name"]).resolve()
    if root not in target.parents or not target.is_file():
        return _error("Tệp lưu trữ không còn tồn tại", 404)
    return FileResponse(target, media_type=item["mime_type"], filename=item["original_name"], headers={"Cache-Control":"private, no-store"})


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
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}", update_document, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}/attachments", upload_attachment, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}/attachments/{attachment_id}", download_attachment, methods=["GET"]),
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}/assign", assign_document, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}/status", transition_document, methods=["PATCH"]),
        Route("/api/v2/organizations/{org_id}/documents/{doc_id}/versions", add_version, methods=["POST"]),
    ]
