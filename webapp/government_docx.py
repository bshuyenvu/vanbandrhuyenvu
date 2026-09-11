from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vbhc_doc_builder import (  # noqa: E402
    Document,
    add_body_paragraph,
    add_header_section,
    add_kinh_gui,
    add_section_heading,
    add_signature_noi_nhan,
    add_so_vb_and_date_section,
    add_title_block,
    apply_page_numbering,
    setup_page,
)

GOVERNMENT_TYPE_NAMES = {
    "gov_bao_cao": "BÁO CÁO",
    "gov_ke_hoach": "KẾ HOẠCH",
    "gov_thong_bao": "THÔNG BÁO",
    "gov_to_trinh": "TỜ TRÌNH",
    "gov_quyet_dinh": "QUYẾT ĐỊNH",
}

DEFAULT_SYMBOL_PREFIX = {
    "gov_bao_cao": "BC",
    "gov_ke_hoach": "KH",
    "gov_thong_bao": "TB",
    "gov_to_trinh": "TTr",
    "gov_quyet_dinh": "QĐ",
}


def _paragraphs(data: dict[str, Any]) -> list[str]:
    raw = data.get("paragraphs") or []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split("\n\n") if p.strip()]
    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text = item.get("text", "")
        else:
            text = item
        if str(text).strip():
            result.append(str(text).strip())
    return result


def build_government_named_document(data: dict[str, Any]) -> bytes:
    """Renderer deterministic cho nhóm văn bản hành chính có tên loại.

    Đây là renderer khung: thể thức Word do vbhc_doc_builder chịu trách nhiệm;
    nội dung/đề mục do payload đã được người dùng hoặc AI duyệt cung cấp.
    """
    type_id = str(data.get("document_type") or "gov_bao_cao")
    ten_loai = GOVERNMENT_TYPE_NAMES.get(type_id)
    if not ten_loai:
        raise ValueError(f"Chưa có renderer cho loại văn bản: {type_id}")

    doc = Document()
    setup_page(doc)
    add_header_section(
        doc,
        co_quan_chu_quan=(data.get("parent_agency") or "CƠ QUAN CHỦ QUẢN").upper(),
        co_quan_ban_hanh=(data.get("agency") or "CƠ QUAN/ĐƠN VỊ").upper(),
    )
    symbol = str(data.get("symbol") or "").strip()
    if not symbol:
        abbr = str(data.get("agency_abbr") or "CQ").strip().upper()
        symbol = f"{DEFAULT_SYMBOL_PREFIX[type_id]}-{abbr}"
    add_so_vb_and_date_section(
        doc,
        so_vb=str(data.get("number") or ""),
        ky_hieu=symbol,
        dia_danh=str(data.get("place") or ""),
        ngay=data.get("day", ""),
        thang=data.get("month", ""),
        nam=data.get("year", ""),
        is_cong_van=False,
    )
    add_title_block(
        doc,
        ten_loai=ten_loai,
        trich_yeu=str(data.get("subject") or "[CẦN BỔ SUNG: TRÍCH YẾU]"),
    )

    if type_id == "gov_to_trinh":
        add_kinh_gui(doc, str(data.get("recipient") or "[CẦN BỔ SUNG: CƠ QUAN NHẬN]"))

    for legal_basis in data.get("legal_bases") or []:
        if str(legal_basis).strip():
            add_body_paragraph(doc, str(legal_basis).strip())

    sections = data.get("sections") or []
    if sections:
        for section in sections:
            heading = str(section.get("heading") or "").strip() if isinstance(section, dict) else ""
            text = str(section.get("text") or "").strip() if isinstance(section, dict) else str(section).strip()
            if heading:
                add_section_heading(doc, heading)
            if text:
                add_body_paragraph(doc, text)
    else:
        for paragraph in _paragraphs(data):
            add_body_paragraph(doc, paragraph)

    add_signature_noi_nhan(
        doc,
        noi_nhan_items=data.get("recipients") or ["- Như trên;"],
        chuc_vu=(data.get("signer_title") or "THỦ TRƯỞNG ĐƠN VỊ").upper(),
        nguoi_ky=str(data.get("signer_name") or ""),
        quyen_han=str(data.get("sign_authority") or ""),
        chuc_vu_thay=str(data.get("sign_for_title") or ""),
        phong_viet_tat=str(data.get("office_abbr") or ""),
    )
    apply_page_numbering(doc, hide_first_page=True)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
