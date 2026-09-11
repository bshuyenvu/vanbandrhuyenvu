from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .api import current_user, require_org_permission
from .document_types import get_document_type
from .security import new_id
from .storage import ensure_storage_capacity, storage_summary
from .store import all_rows, audit, connect, one
from .workflow_api import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    ATTACHMENT_MIME,
    _attachment_root,
    _safe_original_name,
    _valid_attachment_content,
)


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(message: str, status: int = 400, **extra: Any) -> JSONResponse:
    return _json({"ok": False, "error": message, **extra}, status)


def _normalize(value: Any) -> str:
    text = str(value or "").strip().lower().replace("đ", "d")
    text = "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _similarity(a: Any, b: Any) -> float:
    left, right = _normalize(a), _normalize(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("metadata") or "{}")
    except Exception:
        return {}


def _duplicate_candidates(organization_id: str, *, sha256: str = "", source_number: str = "",
                          sender: str = "", subject: str = "", document_date: str = "") -> dict[str, Any]:
    exact: list[dict[str, Any]] = []
    if sha256:
        exact = all_rows("""SELECT d.id,d.intake_number,d.register_year,d.source_number,d.sender,d.subject,d.created_at,a.original_name
            FROM document_attachments a JOIN documents d ON d.id=a.document_id
            WHERE a.organization_id=? AND a.sha256=? ORDER BY d.created_at DESC LIMIT 10""",
                         (organization_id, sha256.lower()))
    recent = all_rows("""SELECT id,intake_number,register_year,source_number,sender,subject,metadata,created_at
        FROM documents WHERE organization_id=? AND direction='incoming'
        ORDER BY created_at DESC LIMIT 300""", (organization_id,))
    possible: list[dict[str, Any]] = []
    wanted_number = _normalize(source_number)
    for row in recent:
        if any(item["id"] == row["id"] for item in exact):
            continue
        meta = _metadata(row)
        score = 0.0
        reasons: list[str] = []
        existing_number = _normalize(row.get("source_number"))
        if wanted_number and existing_number and wanted_number == existing_number:
            score += 0.55
            reasons.append("trùng số/ký hiệu")
        sender_score = _similarity(sender, row.get("sender"))
        if sender_score >= 0.78:
            score += 0.15
            reasons.append("cơ quan ban hành tương đồng")
        subject_score = _similarity(subject, row.get("subject"))
        if subject_score >= 0.72:
            score += 0.22
            reasons.append("trích yếu tương đồng")
        old_date = str(meta.get("document_date") or "").strip()
        if document_date and old_date and _normalize(document_date) == _normalize(old_date):
            score += 0.18
            reasons.append("trùng ngày ban hành")
        if score >= 0.55:
            possible.append({
                "id": row["id"], "intake_number": row.get("intake_number"),
                "register_year": row.get("register_year"), "source_number": row.get("source_number"),
                "sender": row.get("sender"), "subject": row.get("subject"),
                "created_at": row.get("created_at"), "score": round(min(score, 0.99), 2),
                "reasons": reasons,
            })
    possible.sort(key=lambda item: item["score"], reverse=True)
    return {"exact": exact, "possible": possible[:10]}


def _words(value: Any) -> set[str]:
    ignored = {"phong", "khoa", "ban", "don", "vi", "va", "cua", "cac", "theo", "ve", "cho", "xu", "ly"}
    return {w for w in _normalize(value).split() if len(w) >= 3 and w not in ignored}


def _routing_suggestion(organization_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
    departments = all_rows("SELECT id,name,code FROM departments WHERE organization_id=? AND status='active' ORDER BY name", (organization_id,))
    if not departments:
        return {"department_id": None, "department_name": None, "confidence": 0.0, "reason": "Không gian chưa có phòng/khoa để đề xuất"}
    suggested = str(analysis.get("suggested_department") or "").strip()
    if suggested:
        ranked = sorted(
            ((max(_similarity(suggested, d["name"]), _similarity(suggested, d.get("code"))), d) for d in departments),
            key=lambda pair: pair[0], reverse=True,
        )
        if ranked and ranked[0][0] >= 0.62:
            score, dep = ranked[0]
            return {"department_id": dep["id"], "department_name": dep["name"],
                    "confidence": round(min(0.98, 0.65 + score * 0.3), 2),
                    "reason": "Khớp với đơn vị AI đề xuất từ nội dung văn bản"}
    text = " ".join(str(analysis.get(k) or "") for k in ("subject", "task_summary", "summary", "sender"))
    text_words = _words(text)
    direct_scores: list[tuple[float, dict[str, Any]]] = []
    for dep in departments:
        dep_words = _words(f"{dep['name']} {dep.get('code') or ''}")
        overlap = len(text_words & dep_words)
        if overlap:
            direct_scores.append((min(0.85, 0.5 + 0.12 * overlap), dep))
    if direct_scores:
        score, dep = max(direct_scores, key=lambda pair: pair[0])
        return {"department_id": dep["id"], "department_name": dep["name"],
                "confidence": round(score, 2), "reason": "Tên/chức năng đơn vị xuất hiện trong nội dung"}
    historical = all_rows("""SELECT d.department_id,d.subject,d.sender,d.metadata,p.name department_name
        FROM documents d JOIN departments p ON p.id=d.department_id
        WHERE d.organization_id=? AND d.direction='incoming' AND d.department_id IS NOT NULL
        ORDER BY d.created_at DESC LIMIT 400""", (organization_id,))
    by_department: dict[str, dict[str, Any]] = {}
    for row in historical:
        meta = _metadata(row)
        old_words = _words(" ".join([str(row.get("subject") or ""), str(row.get("sender") or ""),
                                     str(meta.get("task_summary") or ""), str(meta.get("summary") or "")]))
        if not old_words or not text_words:
            continue
        common = len(text_words & old_words)
        if not common:
            continue
        similarity = common / max(3.0, (len(text_words) * len(old_words)) ** 0.5)
        bucket = by_department.setdefault(row["department_id"], {"score": 0.0, "hits": 0, "name": row["department_name"]})
        bucket["score"] += similarity
        bucket["hits"] += 1
    if by_department:
        dep_id, bucket = max(by_department.items(), key=lambda pair: (pair[1]["score"], pair[1]["hits"]))
        confidence = min(0.78, 0.38 + bucket["score"] * 0.12 + min(bucket["hits"], 4) * 0.05)
        if confidence >= 0.5:
            return {"department_id": dep_id, "department_name": bucket["name"],
                    "confidence": round(confidence, 2),
                    "reason": f"Tương đồng với {bucket['hits']} hồ sơ đã xử lý trước đây"}
    return {"department_id": None, "department_name": None, "confidence": 0.0,
            "reason": "Chưa đủ dữ liệu để đề xuất đơn vị xử lý"}


def _require_create(request: Request) -> tuple[dict[str, Any] | None, str, JSONResponse | None]:
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user:
        return None, org_id, _error("Chưa đăng nhập", 401)
    if not require_org_permission(user, org_id, "document.create")[0]:
        return None, org_id, _error("Không có quyền tiếp nhận văn bản", 403)
    return user, org_id, None


async def preflight(request: Request):
    user, org_id, error = _require_create(request)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _error("Dữ liệu yêu cầu phải là JSON")
    sha256 = str(body.get("sha256") or "").strip().lower()
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return _error("Mã kiểm tra SHA-256 không hợp lệ")
    try:
        size_bytes = max(0, int(body.get("size_bytes") or 0))
    except (TypeError, ValueError):
        return _error("Kích thước tệp không hợp lệ")
    max_bytes = max(1024 * 1024, int(os.getenv("VBHC_ATTACHMENT_MAX_BYTES", str(12 * 1024 * 1024))))
    if size_bytes > max_bytes:
        return _error(f"Tệp vượt quá {max_bytes // (1024 * 1024)} MB", 413)
    try:
        capacity = ensure_storage_capacity(user_id=user["id"], organization_id=org_id, incoming_bytes=size_bytes)
    except PermissionError as exc:
        return _error(str(exc), 403)
    except ValueError as exc:
        return _error(str(exc), 413)
    duplicates = _duplicate_candidates(org_id, sha256=sha256,
        source_number=str(body.get("source_number") or ""), sender=str(body.get("sender") or ""),
        subject=str(body.get("subject") or ""), document_date=str(body.get("document_date") or ""))
    return _json({"ok": True, "duplicates": duplicates, "storage": capacity,
                  "safe_to_analyze": not bool(duplicates["exact"])})


async def suggest_routing(request: Request):
    user, org_id, error = _require_create(request)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _error("Dữ liệu yêu cầu phải là JSON")
    analysis = body.get("analysis") if isinstance(body.get("analysis"), dict) else body
    return _json({"ok": True, "suggestion": _routing_suggestion(org_id, analysis)})


def _decode_upload(body: dict[str, Any]) -> tuple[str, str, bytes, str, str]:
    original = _safe_original_name(str(body.get("filename") or "document"))
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise ValueError("Chỉ cho phép PDF, DOCX, TXT, PNG, JPG/JPEG hoặc WEBP")
    try:
        raw = base64.b64decode(str(body.get("file_base64") or ""), validate=True)
    except Exception as exc:
        raise ValueError("Dữ liệu tệp mã hóa không hợp lệ") from exc
    max_bytes = max(1024 * 1024, int(os.getenv("VBHC_ATTACHMENT_MAX_BYTES", str(12 * 1024 * 1024))))
    if not raw:
        raise ValueError("Tệp rỗng")
    if len(raw) > max_bytes:
        raise OverflowError(f"Tệp vượt quá {max_bytes // (1024 * 1024)} MB")
    if not _valid_attachment_content(ext, raw):
        raise ValueError("Nội dung tệp không khớp định dạng hoặc tệp bị hỏng")
    digest = hashlib.sha256(raw).hexdigest()
    supplied = str(body.get("sha256") or "").strip().lower()
    if supplied and supplied != digest:
        raise ValueError("Mã SHA-256 không khớp nội dung tệp")
    return original, ext, raw, digest, ATTACHMENT_MIME[ext]


def _validate_assignment(user: dict[str, Any], org_id: str, department_id: str | None,
                         assignee_user_id: str | None) -> str | None:
    if not department_id and not assignee_user_id:
        return None
    if not require_org_permission(user, org_id, "document.assign")[0]:
        return "Không có quyền phân công văn bản"
    if department_id and not one("SELECT id FROM departments WHERE id=? AND organization_id=? AND status='active'", (department_id, org_id)):
        return "Phòng/khoa được chọn không hợp lệ"
    if assignee_user_id and not one("SELECT id FROM memberships WHERE organization_id=? AND user_id=? AND status='active'", (org_id, assignee_user_id)):
        return "Người được giao không phải thành viên đang hoạt động"
    return None


async def commit_intake(request: Request):
    user, org_id, error = _require_create(request)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _error("Dữ liệu yêu cầu phải là JSON")
    try:
        original, ext, raw, digest, mime = _decode_upload(body)
    except OverflowError as exc:
        return _error(str(exc), 413)
    except ValueError as exc:
        return _error(str(exc), 422)
    type_id = str(body.get("document_type") or "gov_cong_van").strip()
    doc_type = get_document_type(type_id)
    if not doc_type or "incoming" not in doc_type.direction:
        return _error("Loại văn bản không phù hợp với văn bản đến")
    subject = str(body.get("subject") or "").strip()
    if len(subject) < 2:
        return _error("Cần nhập trích yếu văn bản")
    priority = str(body.get("priority") or "normal").strip()
    confidentiality = str(body.get("confidentiality") or "internal").strip()
    if priority not in {"normal", "urgent", "very_urgent"}:
        return _error("Mức độ ưu tiên không hợp lệ")
    if confidentiality not in {"public", "internal", "confidential", "restricted"}:
        return _error("Mức độ dữ liệu không hợp lệ")
    department_id = str(body.get("department_id") or "").strip() or None
    assignee_user_id = str(body.get("assignee_user_id") or "").strip() or None
    assignment_error = _validate_assignment(user, org_id, department_id, assignee_user_id)
    if assignment_error:
        return _error(assignment_error, 403)
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    document_date = str(metadata.get("document_date") or body.get("document_date") or "").strip()
    duplicates = _duplicate_candidates(
        org_id, sha256=digest, source_number=str(body.get("source_number") or ""),
        sender=str(body.get("sender") or ""), subject=subject, document_date=document_date,
    )
    if duplicates["exact"]:
        return _error("Tệp này đã tồn tại trong sổ văn bản", 409, code="duplicate_file", duplicates=duplicates)
    if duplicates["possible"] and not bool(body.get("duplicate_confirmed")):
        return _error("Phát hiện hồ sơ có khả năng trùng. Cần kiểm tra trước khi ghi sổ.", 409,
                      code="possible_duplicate", duplicates=duplicates)
    try:
        capacity = ensure_storage_capacity(user_id=user["id"], organization_id=org_id, incoming_bytes=len(raw))
    except PermissionError as exc:
        return _error(str(exc), 403)
    except ValueError as exc:
        return _error(str(exc), 413)
    root = _attachment_root()
    doc_id, attachment_id = new_id("doc_"), new_id("att_")
    target_dir = (root / org_id / doc_id).resolve()
    if root not in target_dir.parents:
        return _error("Đường dẫn lưu tệp không hợp lệ", 500)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stored_name = attachment_id + ext
    target = target_dir / stored_name
    pending = target_dir / (stored_name + ".pending")
    pending.write_bytes(raw)
    pending.chmod(0o600)
    metadata = {**metadata, "intake_source": str(body.get("intake_source") or "batch_ocr"),
                "source_sha256": digest, "source_filename": original}
    assignment_id = None
    intake_number = register_year = received_at = None
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM document_attachments WHERE organization_id=? AND sha256=? LIMIT 1", (org_id, digest)).fetchone():
                raise FileExistsError("Tệp này vừa được ghi sổ bởi một phiên khác")
            total = capacity.get("total_bytes")
            used = int(db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM document_attachments WHERE organization_id=?", (org_id,)).fetchone()[0] or 0)
            if total is not None and used + len(raw) > int(total):
                raise OverflowError("Dung lượng hồ sơ đã đạt giới hạn của gói")
            register_year = int(db.execute("SELECT CAST(strftime('%Y','now') AS INTEGER)").fetchone()[0])
            intake_number = int(db.execute("SELECT COALESCE(MAX(intake_number),0)+1 FROM documents WHERE organization_id=? AND direction='incoming' AND register_year=?", (org_id, register_year)).fetchone()[0])
            received_at = db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
            status = "assigned" if (department_id or assignee_user_id) else "received"
            db.execute("""INSERT INTO documents(id,organization_id,department_id,direction,document_type,standard,source_number,symbol,sender,subject,status,priority,confidentiality,deadline,owner_user_id,intake_number,register_year,received_at,metadata)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (doc_id, org_id, department_id, "incoming", type_id, doc_type.standard,
                 body.get("source_number"), body.get("symbol"), body.get("sender"), subject, status,
                 priority, confidentiality, body.get("deadline") or None, user["id"], intake_number,
                 register_year, received_at, json.dumps(metadata, ensure_ascii=False)))
            db.execute("""INSERT INTO document_attachments(id,document_id,organization_id,original_name,stored_name,mime_type,size_bytes,sha256,uploaded_by)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (attachment_id, doc_id, org_id, original, stored_name, mime, len(raw), digest, user["id"]))
            if department_id or assignee_user_id:
                assignment_id = new_id("asg_")
                db.execute("""INSERT INTO document_assignments(id,document_id,organization_id,department_id,assignee_user_id,assignment_role,status,due_at,assigned_by,note)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (assignment_id, doc_id, org_id, department_id, assignee_user_id, "primary", "assigned",
                     body.get("deadline") or None, user["id"], body.get("assignment_note") or "Phân công từ cổng tiếp nhận & số hóa"))
            pending.replace(target)
            db.commit()
    except FileExistsError as exc:
        pending.unlink(missing_ok=True)
        return _error(str(exc), 409, code="duplicate_file")
    except OverflowError as exc:
        pending.unlink(missing_ok=True)
        return _error(str(exc), 413)
    except Exception as exc:
        pending.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        return _error(f"Không thể ghi sổ văn bản: {exc}", 500)
    audit(organization_id=org_id, user_id=user["id"], action="document.intake.commit",
          entity_type="document", entity_id=doc_id,
          after={"intake_number": intake_number, "register_year": register_year,
                 "attachment_id": attachment_id, "sha256": digest, "department_id": department_id})
    return _json({"ok": True, "document_id": doc_id, "intake_number": intake_number,
                  "register_year": register_year, "received_at": received_at,
                  "attachment_id": attachment_id, "sha256": digest,
                  "assignment_id": assignment_id}, 201)


def routes() -> list[Route]:
    return [
        Route("/api/v2/organizations/{org_id}/intake/preflight", preflight, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/intake/suggest-routing", suggest_routing, methods=["POST"]),
        Route("/api/v2/organizations/{org_id}/intake/commit", commit_intake, methods=["POST"]),
    ]
