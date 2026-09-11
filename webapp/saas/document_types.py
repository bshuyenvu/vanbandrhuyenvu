from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class DocumentType:
    id: str
    name: str
    standard: str
    direction: tuple[str, ...]
    renderer: str | None
    workflow: str
    enabled_export: bool = False

DOCUMENT_TYPES: dict[str, DocumentType] = {
    "gov_cong_van": DocumentType("gov_cong_van", "Công văn", "government", ("incoming","outgoing"), "government_reply", "approval_standard", True),
    "gov_bao_cao": DocumentType("gov_bao_cao", "Báo cáo", "government", ("incoming","outgoing","internal"), "government_named", "approval_standard", True),
    "gov_to_trinh": DocumentType("gov_to_trinh", "Tờ trình", "government", ("incoming","outgoing"), "government_named", "approval_standard", True),
    "gov_ke_hoach": DocumentType("gov_ke_hoach", "Kế hoạch", "government", ("incoming","outgoing","internal"), "government_named", "approval_standard", True),
    "gov_quyet_dinh": DocumentType("gov_quyet_dinh", "Quyết định", "government", ("incoming","outgoing"), "government_named", "approval_strict", True),
    "gov_thong_bao": DocumentType("gov_thong_bao", "Thông báo", "government", ("incoming","outgoing","internal"), "government_named", "approval_standard", True),
    "gov_bien_ban": DocumentType("gov_bien_ban", "Biên bản", "government", ("incoming","internal"), "government_minutes", "approval_light"),
    "gov_giay_moi": DocumentType("gov_giay_moi", "Giấy mời", "government", ("incoming","outgoing"), "government_invitation", "approval_light"),
    "party_cong_van": DocumentType("party_cong_van", "Công văn Đảng", "party", ("incoming","outgoing"), "party_reply", "party_approval", True),
    "party_bao_cao": DocumentType("party_bao_cao", "Báo cáo Đảng", "party", ("incoming","outgoing","internal"), "party_report", "party_approval"),
    "party_to_trinh": DocumentType("party_to_trinh", "Tờ trình Đảng", "party", ("incoming","outgoing"), "party_submission", "party_approval"),
    "party_ke_hoach": DocumentType("party_ke_hoach", "Kế hoạch Đảng", "party", ("incoming","outgoing","internal"), "party_plan", "party_approval"),
    "party_quyet_dinh": DocumentType("party_quyet_dinh", "Quyết định Đảng", "party", ("incoming","outgoing"), "party_decision", "party_approval_strict"),
    "party_thong_bao": DocumentType("party_thong_bao", "Thông báo Đảng", "party", ("incoming","outgoing","internal"), "party_notice", "party_approval"),
    "party_nghi_quyet": DocumentType("party_nghi_quyet", "Nghị quyết", "party", ("incoming","outgoing","internal"), "party_resolution", "party_approval_strict"),
    "party_ket_luan": DocumentType("party_ket_luan", "Kết luận", "party", ("incoming","outgoing","internal"), "party_conclusion", "party_approval_strict"),
}


def list_document_types(*, standard: str | None = None, direction: str | None = None) -> list[dict]:
    out = []
    for item in DOCUMENT_TYPES.values():
        if standard and item.standard != standard:
            continue
        if direction and direction not in item.direction:
            continue
        out.append({
            "id": item.id, "name": item.name, "standard": item.standard,
            "direction": list(item.direction), "renderer": item.renderer,
            "workflow": item.workflow, "enabled_export": item.enabled_export,
        })
    return out


def get_document_type(type_id: str) -> DocumentType | None:
    return DOCUMENT_TYPES.get(type_id)
