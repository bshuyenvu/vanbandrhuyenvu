from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .catalog import MODEL_DEFAULTS, PLANS

TASK_DEFAULT_TIER = {
    "classify": "economy",
    "extract": "economy",
    "ocr": "economy",
    "summarize": "economy",
    "draft": "standard",
    "review": "standard",
    "legal_review": "advanced",
    "complex_reasoning": "advanced",
}

TIER_ORDER = ["economy", "standard", "advanced", "private"]

@dataclass(frozen=True)
class ModelChoice:
    model_id: str
    provider: str
    model_name: str
    display_name: str
    tier: str
    reason: str


def _cost_score(model: dict[str, Any]) -> float:
    """Simple blended cost score; admin prices immediately affect routing."""
    inp = float(model.get("input_usd_per_million") or 0)
    out = float(model.get("output_usd_per_million") or 0)
    mult = float(model.get("service_multiplier") or 1)
    return (inp + out * 0.35) * mult


def _within_daily_budget(model_id: str, model: dict[str, Any]) -> bool:
    budget = model.get("daily_budget_usd")
    if budget is None or float(budget or 0) <= 0:
        return True
    try:
        from .store import one
        row = one("SELECT COALESCE(SUM(provider_cost_usd),0) spent FROM ai_usage WHERE model_id=? AND date(created_at)=date('now')", (model_id,))
        spent = float((row or {}).get("spent") or 0)
        return spent < float(budget)
    except Exception:
        # If an administrator configured a hard budget, fail closed when spend cannot be read.
        return False


def _eligible(model_id: str, model: dict[str, Any], *, target: str, data_policy: str) -> bool:
    if not bool(model.get("enabled", True)):
        return False
    if str(model.get("tier", "economy")) != target:
        return False
    if data_policy == "restricted" and str(model.get("provider")) not in {"local", "private"}:
        return False
    return _within_daily_budget(model_id, model)


def choose_model(*, task_type: str, plan_id: str, data_policy: str = "internal",
                 requested_tier: str | None = None, registry: dict[str, dict[str, Any]] | None = None) -> ModelChoice:
    registry = registry or MODEL_DEFAULTS
    plan = PLANS.get(plan_id, PLANS["free"])
    allowed = set(plan.get("tiers", ["economy"]))
    target = requested_tier or TASK_DEFAULT_TIER.get(task_type, "economy")

    if data_policy == "restricted":
        target = "private"
    elif target not in allowed:
        target = max((t for t in TIER_ORDER if t in allowed), key=TIER_ORDER.index, default="economy")

    candidates = [(model_id, model) for model_id, model in registry.items() if _eligible(model_id, model, target=target, data_policy=data_policy)]

    if not candidates and target != "economy" and "economy" in allowed and data_policy != "restricted":
        target = "economy"
        candidates = [(model_id, model) for model_id, model in registry.items() if _eligible(model_id, model, target=target, data_policy=data_policy)]

    if not candidates:
        raise ValueError("Không có mô hình AI khả dụng: hãy kiểm tra gói, chính sách dữ liệu, trạng thái mô hình hoặc ngân sách ngày")

    candidates.sort(key=lambda item: _cost_score(item[1]))
    model_id, model = candidates[0]
    return ModelChoice(
        model_id=model_id,
        provider=str(model.get("provider")),
        model_name=str(model.get("model_name")),
        display_name=str(model.get("display_name")),
        tier=target,
        reason=f"task={task_type}; plan={plan_id}; policy={data_policy}; strategy=lowest_cost_in_tier; budget=ok",
    )


def public_model_catalog(plan_id: str, registry: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    plan = PLANS.get(plan_id, PLANS["free"])
    allowed = set(plan.get("tiers", []))
    registry = registry or MODEL_DEFAULTS
    result = []
    for model_id, model in registry.items():
        tier = str(model.get("tier", "economy"))
        if tier not in allowed or not _eligible(model_id, model, target=tier, data_policy="internal"):
            continue
        result.append({
            "id": model_id,
            "display_name": model.get("display_name"),
            "provider": model.get("provider"),
            "tier": tier,
            "cost_score": _cost_score(model),
        })
    return sorted(result, key=lambda item: (TIER_ORDER.index(item["tier"]), item["cost_score"]))
