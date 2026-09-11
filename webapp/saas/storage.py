from __future__ import annotations

from typing import Any

from .catalog import PLANS
from .store import active_plan, one


def _used_bytes(organization_id: str) -> int:
    row = one("SELECT COALESCE(SUM(size_bytes),0) n FROM document_attachments WHERE organization_id=?", (organization_id,)) or {"n": 0}
    return int(row.get("n") or 0)


def storage_summary(*, user_id: str, organization_id: str) -> dict[str, Any]:
    org = one("SELECT id,name,owner_user_id,workspace_type,storage_quota_bytes FROM organizations WHERE id=?", (organization_id,))
    if not org:
        raise ValueError("Không tìm thấy không gian lưu trữ")
    personal = str(org.get("workspace_type") or "organization") == "personal"
    if personal:
        if str(org.get("owner_user_id") or "") != user_id:
            raise PermissionError("Kho cá nhân không thuộc tài khoản này")
        plan_id = active_plan("user", user_id)
        total = PLANS.get(plan_id, PLANS["free"]).get("storage_bytes")
        scope = "personal"
    else:
        sub = one("SELECT plan_id FROM subscriptions WHERE scope_type='organization' AND scope_id=? AND status='active' AND (ends_at IS NULL OR ends_at>CURRENT_TIMESTAMP) ORDER BY COALESCE(starts_at,created_at) DESC LIMIT 1", (organization_id,))
        plan_id = str(sub["plan_id"]) if sub else "organization"
        custom = org.get("storage_quota_bytes")
        total = int(custom) if custom is not None else PLANS.get(plan_id, PLANS["organization"]).get("storage_bytes")
        scope = "organization"
    used = _used_bytes(organization_id)
    total_int = None if total is None else int(total)
    remaining = None if total_int is None else max(0, total_int - used)
    percent = 0 if not total_int else min(100.0, round(used * 100 / total_int, 2))
    return {
        "organization_id": organization_id, "workspace_name": org.get("name"),
        "scope": scope, "plan_id": plan_id, "plan_name": PLANS.get(plan_id, PLANS["free"])["name"],
        "used_bytes": used, "total_bytes": total_int, "remaining_bytes": remaining, "percent_used": percent,
    }


def ensure_storage_capacity(*, user_id: str, organization_id: str, incoming_bytes: int) -> dict[str, Any]:
    summary = storage_summary(user_id=user_id, organization_id=organization_id)
    total = summary["total_bytes"]
    if total is not None and summary["used_bytes"] + int(incoming_bytes) > total:
        raise ValueError("Dung lượng hồ sơ đã đạt giới hạn của gói. Vui lòng xóa bớt hồ sơ hoặc nâng gói.")
    return summary
