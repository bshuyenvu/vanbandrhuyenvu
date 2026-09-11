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

    candidates = []
    for model_id, model in registry.items():
        if not model.get("enabled", True):
            continue
        tier = model.get("tier", "economy")
        if tier != target:
            continue
        if data_policy == "restricted" and model.get("provider") not in {"local", "private"}:
            continue
        candidates.append((model_id, model))

    if not candidates and target != "economy" and "economy" in allowed and data_policy != "restricted":
        candidates = [(mid, m) for mid, m in registry.items() if m.get("enabled", True) and m.get("tier") == "economy"]
        target = "economy"

    if not candidates:
        raise ValueError("Không có model phù hợp với gói sử dụng và chính sách dữ liệu")

    model_id, model = candidates[0]
    return ModelChoice(
        model_id=model_id,
        provider=str(model.get("provider")),
        model_name=str(model.get("model_name")),
        display_name=str(model.get("display_name")),
        tier=target,
        reason=f"task={task_type}; plan={plan_id}; policy={data_policy}",
    )


def public_model_catalog(plan_id: str) -> list[dict[str, Any]]:
    plan = PLANS.get(plan_id, PLANS["free"])
    allowed = set(plan.get("tiers", []))
    result = []
    for model_id, model in MODEL_DEFAULTS.items():
        if model.get("tier") not in allowed:
            continue
        result.append({
            "id": model_id,
            "display_name": model.get("display_name"),
            "provider": model.get("provider"),
            "tier": model.get("tier"),
        })
    return result
