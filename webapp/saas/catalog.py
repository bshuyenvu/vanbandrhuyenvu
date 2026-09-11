from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

PROJECT_NAME = "Huyền Vũ Văn Bản AI"

PLANS = {
    "free": {
        "name": "Miễn phí", "price_vnd": 0, "included_credits": 10000,
        "monthly_requests": 30, "monthly_tokens": 200000, "storage_bytes": 100 * 1024 * 1024,
        "max_members": 1, "tiers": ["economy"],
        "features": ["personal_workspace", "incoming_outgoing_basic", "ai_draft_limited"]
    },
    "personal": {
        "name": "Cá nhân", "price_vnd": 79000, "included_credits": 90000,
        "monthly_requests": 300, "monthly_tokens": 2000000, "storage_bytes": 1 * 1024 * 1024 * 1024,
        "max_members": 1, "tiers": ["economy", "standard"],
        "features": ["personal_workspace", "document_versions", "legal_rag_basic", "priority_queue"]
    },
    "professional": {
        "name": "Chuyên nghiệp", "price_vnd": 179000, "included_credits": 240000,
        "monthly_requests": 1500, "monthly_tokens": 8000000, "storage_bytes": 5 * 1024 * 1024 * 1024,
        "max_members": 1, "tiers": ["economy", "standard", "advanced"],
        "features": ["advanced_review", "legal_rag", "model_choice", "export_templates"]
    },
    "team": {
        "name": "Nhóm", "price_vnd": 499000, "included_credits": 650000,
        "monthly_requests": 5000, "monthly_tokens": 25000000, "storage_bytes": 20 * 1024 * 1024 * 1024,
        "max_members": 10, "tiers": ["economy", "standard", "advanced"],
        "features": ["departments", "shared_wallet", "assignments", "audit", "admin_console"]
    },
    "organization": {
        "name": "Cơ quan", "price_vnd": 0, "included_credits": 0,
        "monthly_requests": None, "monthly_tokens": None, "storage_bytes": None,
        "max_members": None, "tiers": ["economy", "standard", "advanced", "private"],
        "features": ["multi_department", "workflow", "budget_guard", "sso_ready", "private_policy"]
    },
}

MODEL_DEFAULTS = {
    "gemini-flash-lite": {
        "provider": "google", "model_name": "gemini-3.5-flash-lite",
        "display_name": "Gemini 3.5 Flash-Lite", "tier": "economy",
        "input_usd_per_million": Decimal("0.30"), "cached_input_usd_per_million": Decimal("0.03"),
        "output_usd_per_million": Decimal("2.50"), "service_multiplier": Decimal("1.30"), "enabled": True
    },
    "gemini-flash": {
        "provider": "google", "model_name": "gemini-3.5-flash",
        "display_name": "Gemini 3.5 Flash", "tier": "standard",
        "input_usd_per_million": Decimal("1.50"), "cached_input_usd_per_million": Decimal("0.15"),
        "output_usd_per_million": Decimal("9.00"), "service_multiplier": Decimal("1.30"), "enabled": True
    },
    "gpt-luna": {
        "provider": "openai", "model_name": "gpt-5.6-luna",
        "display_name": "GPT-5.6 Luna", "tier": "standard",
        "input_usd_per_million": Decimal("0.20"), "cached_input_usd_per_million": Decimal("0.02"),
        "output_usd_per_million": Decimal("1.20"), "service_multiplier": Decimal("1.30"), "enabled": True
    },
    "gpt-terra": {
        "provider": "openai", "model_name": "gpt-5.6-terra",
        "display_name": "GPT-5.6 Terra", "tier": "advanced",
        "input_usd_per_million": Decimal("2.00"), "cached_input_usd_per_million": Decimal("0.20"),
        "output_usd_per_million": Decimal("12.00"), "service_multiplier": Decimal("1.30"), "enabled": True
    },
}

@dataclass(frozen=True)
class UsageQuote:
    provider_cost_usd: Decimal
    estimated_vnd: int
    charged_credits: int


def quote_usage(*, input_tokens: int, output_tokens: int, model: dict,
                cached_tokens: int = 0, usd_vnd: Decimal = Decimal("27000"),
                reserve_multiplier: Decimal = Decimal("1.10")) -> UsageQuote:
    regular_input = max(0, input_tokens - cached_tokens)
    million = Decimal(1_000_000)
    cost = (
        Decimal(regular_input) / million * Decimal(str(model.get("input_usd_per_million", 0)))
        + Decimal(max(0, cached_tokens)) / million * Decimal(str(model.get("cached_input_usd_per_million", 0)))
        + Decimal(max(0, output_tokens)) / million * Decimal(str(model.get("output_usd_per_million", 0)))
    )
    service = Decimal(str(model.get("service_multiplier", 1)))
    estimated_vnd = int(math.ceil(cost * usd_vnd * reserve_multiplier * service))
    return UsageQuote(provider_cost_usd=cost, estimated_vnd=estimated_vnd, charged_credits=max(1, estimated_vnd))
