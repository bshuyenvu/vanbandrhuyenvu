from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .api import current_user, org_role, platform_admin
from .store import all_rows, one
from .workflow_api import can_access_document

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
COMPLETED = {"issued", "closed", "archived", "cancelled"}


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(message: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": message}, status)


def _parse_deadline(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(VN_TZ) if dt.tzinfo else dt.replace(tzinfo=VN_TZ)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=VN_TZ)
        except ValueError:
            continue
    return None


def _deadline_state(deadline: datetime | None, status: str, now: datetime) -> tuple[str, int | None]:
    if not deadline or status in COMPLETED:
        return "khong_canh_bao", None
    seconds = int((deadline - now).total_seconds())
    hours = int(seconds / 3600)
    if seconds < 0:
        return "qua_han", hours
    if seconds <= 24 * 3600:
        return "sap_han_24h", hours
    if seconds <= 48 * 3600:
        return "sap_han_48h", hours
    return "binh_thuong", hours


def _bucket(status: str, has_assignment: bool) -> str:
    if status == "received" and not has_assignment:
        return "cho_giao"
    if status in {"assigned", "processing", "rejected"}:
        return "dang_xu_ly"
    if status == "submitted":
        return "cho_duyet"
    if status == "approved":
        return "cho_phat_hanh"
    if status in COMPLETED:
        return "hoan_tat"
    return "khac"


def _latest_assignments(org_id: str) -> dict[str, dict[str, Any]]:
    rows = all_rows("""SELECT a.*,d.name department_name,u.full_name assignee_name,u.email assignee_email
        FROM document_assignments a
        LEFT JOIN departments d ON d.id=a.department_id
        LEFT JOIN users u ON u.id=a.assignee_user_id
        WHERE a.organization_id=? ORDER BY a.created_at DESC""", (org_id,))
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(str(row["document_id"]), row)
    return result


def _item(doc: dict[str, Any], assignment: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    deadline_raw = (assignment or {}).get("due_at") or doc.get("deadline")
    deadline = _parse_deadline(deadline_raw)
    state, hours = _deadline_state(deadline, str(doc.get("status") or ""), now)
    return {
        "id": doc["id"], "direction": doc.get("direction"),
        "source_number": doc.get("source_number"), "register_number": doc.get("register_number"),
        "symbol": doc.get("symbol"), "subject": doc.get("subject"), "sender": doc.get("sender"),
        "recipient": doc.get("recipient"), "status": doc.get("status"), "priority": doc.get("priority"),
        "intake_number": doc.get("intake_number"), "register_year": doc.get("register_year"),
        "department_id": (assignment or {}).get("department_id") or doc.get("department_id"),
        "department_name": (assignment or {}).get("department_name"),
        "assignee_user_id": (assignment or {}).get("assignee_user_id"),
        "assignee_name": (assignment or {}).get("assignee_name") or (assignment or {}).get("assignee_email"),
        "deadline": deadline.isoformat() if deadline else deadline_raw,
        "deadline_state": state, "hours_remaining": hours,
        "bucket": _bucket(str(doc.get("status") or ""), bool(assignment)),
        "created_at": doc.get("created_at"), "updated_at": doc.get("updated_at"),
    }

async def work_queue(request: Request):
    user = current_user(request)
    org_id = request.path_params["org_id"]
    if not user or (not platform_admin(user) and not org_role(user["id"], org_id)):
        return _error("Không có quyền xem công việc của không gian này", 403)
    docs = all_rows("""SELECT * FROM documents WHERE organization_id=?
        ORDER BY updated_at DESC, created_at DESC LIMIT 500""", (org_id,))
    latest = _latest_assignments(org_id)
    now = datetime.now(VN_TZ)
    items = [_item(doc, latest.get(str(doc["id"])), now) for doc in docs
             if can_access_document(user, org_id, str(doc["id"]))]
    bucket_order = {"cho_giao": 0, "qua_han": 1, "cho_duyet": 2, "dang_xu_ly": 3,
                    "cho_phat_hanh": 4, "khac": 5, "hoan_tat": 6}
    items.sort(key=lambda x: (
        0 if x["deadline_state"] == "qua_han" else 1,
        x["deadline"] or "9999-12-31", bucket_order.get(x["bucket"], 9),
    ))
    counts = {key: sum(1 for item in items if item["bucket"] == key) for key in
              ("cho_giao", "dang_xu_ly", "cho_duyet", "cho_phat_hanh", "hoan_tat")}
    alerts = {
        "qua_han": sum(1 for item in items if item["deadline_state"] == "qua_han"),
        "sap_han_24h": sum(1 for item in items if item["deadline_state"] == "sap_han_24h"),
        "sap_han_48h": sum(1 for item in items if item["deadline_state"] == "sap_han_48h"),
    }
    urgent = [item for item in items if item["deadline_state"] in {"qua_han", "sap_han_24h", "sap_han_48h"}]
    return _json({"ok": True, "items": items, "counts": counts, "alerts": alerts,
                  "urgent": urgent[:50], "server_time": now.isoformat()})


def routes() -> list[Route]:
    return [Route("/api/v2/organizations/{org_id}/work-queue", work_queue, methods=["GET"])]
