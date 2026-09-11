from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .catalog import PLANS
from .store import all_rows, one


def _calendar_period() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.isoformat()


def _period(*, user_id: str, plan_id: str, organization_id: str | None) -> tuple[str, str]:
    if plan_id != "free":
        scope_type, scope_id = ("organization", organization_id) if organization_id else ("user", user_id)
        row = one("SELECT COALESCE(starts_at,created_at) starts_at,ends_at FROM subscriptions WHERE scope_type=? AND scope_id=? AND plan_id=? AND status='active' AND (ends_at IS NULL OR ends_at>CURRENT_TIMESTAMP) ORDER BY COALESCE(starts_at,created_at) DESC LIMIT 1", (scope_type,scope_id,plan_id))
        if row and row.get("ends_at"):
            start = str(row.get("starts_at") or "")
            end = str(row.get("ends_at") or "").replace(" ", "T") + "Z"
            return start, end
    return _calendar_period()


def _aggregate(where: str, params: tuple[Any, ...], *, start: str | None = None) -> dict[str, Any]:
    clause = where + (" AND created_at >= ?" if start else "")
    values = params + ((start,) if start else ())
    row = one(
        f"""SELECT COUNT(*) requests,
        COALESCE(SUM(input_tokens),0) input_tokens,
        COALESCE(SUM(cached_tokens),0) cached_tokens,
        COALESCE(SUM(output_tokens),0) output_tokens,
        COALESCE(SUM(charged_credits),0) charged_credits,
        COALESCE(SUM(provider_cost_usd),0) provider_cost_usd
        FROM ai_usage WHERE {clause}""",
        values,
    ) or {}
    row["total_tokens"] = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
    return row


def usage_summary(*, user_id: str, plan_id: str, wallet: dict[str, Any],
                  organization_id: str | None = None) -> dict[str, Any]:
    plan = PLANS.get(plan_id, PLANS["free"])
    period_start, reset_at = _period(user_id=user_id, plan_id=plan_id, organization_id=organization_id)
    monthly = _aggregate("wallet_id=?", (wallet["id"],), start=period_start)
    lifetime = _aggregate("wallet_id=?", (wallet["id"],), start=None)

    if organization_id:
        mine = _aggregate("user_id=? AND organization_id=?", (user_id, organization_id), start=period_start)
    else:
        mine = _aggregate("user_id=? AND organization_id IS NULL", (user_id,), start=period_start)

    request_limit = plan.get("monthly_requests")
    token_limit = plan.get("monthly_tokens")
    used_requests = int(monthly.get("requests") or 0)
    used_tokens = int(monthly.get("total_tokens") or 0)
    return {
        "plan_id": plan_id,
        "plan_name": plan.get("name", plan_id),
        "scope": "organization" if organization_id else "personal",
        "organization_id": organization_id,
        "wallet_balance_credits": int(wallet.get("balance_credits") or 0),
        "quota": {
            "requests_total": request_limit,
            "requests_used": used_requests,
            "requests_remaining": None if request_limit is None else max(0, int(request_limit) - used_requests),
            "tokens_total": token_limit,
            "tokens_used": used_tokens,
            "tokens_remaining": None if token_limit is None else max(0, int(token_limit) - used_tokens),
            "period_start": period_start.replace(" ", "T") + ("Z" if "T" not in period_start else ""),
            "reset_at": reset_at,
        },
        "month": monthly,
        "my_month": mine,
        "lifetime": lifetime,
    }


def ensure_usage_quota(*, user_id: str, plan_id: str, wallet: dict[str, Any],
                       organization_id: str | None = None) -> None:
    summary = usage_summary(user_id=user_id, plan_id=plan_id, wallet=wallet, organization_id=organization_id)
    q = summary["quota"]
    if q["requests_total"] is not None and q["requests_remaining"] <= 0:
        raise ValueError("Đã hết lượt AI của kỳ hiện tại. Vui lòng chờ kỳ mới hoặc nâng gói.")
    if q["tokens_total"] is not None and q["tokens_remaining"] <= 0:
        raise ValueError("Đã hết quota token của kỳ hiện tại. Vui lòng chờ kỳ mới hoặc nâng gói.")
