from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def build_government_reply(data: dict[str, Any]) -> bytes:
    from vbhc_doc_builder import (
        Document,
        add_body_paragraph,
        add_header_section,
        add_kinh_gui,
        add_signature_noi_nhan,
        add_so_vb_and_date_section,
        apply_page_numbering,
        setup_page,
    )

    doc = Document()
    setup_page(doc)
    add_header_section(
        doc,
        co_quan_chu_quan=(data.get("parent_agency") or "CƠ QUAN CHỦ QUẢN").upper(),
        co_quan_ban_hanh=(data.get("agency") or "CƠ QUAN/ĐƠN VỊ").upper(),
    )
    add_so_vb_and_date_section(
        doc,
        so_vb=data.get("number", ""),
        ky_hieu=data.get("symbol", "CV"),
        trich_yeu=data.get("subject", "Phúc đáp văn bản"),
        dia_danh=data.get("place", ""),
        ngay=data.get("day", ""),
        thang=data.get("month", ""),
        nam=data.get("year", ""),
        is_cong_van=True,
    )
    add_kinh_gui(doc, data.get("recipient") or "Cơ quan gửi văn bản")
    paragraphs = data.get("paragraphs") or []
    if isinstance(paragraphs, str):
        paragraphs = [x.strip() for x in paragraphs.split("\n\n") if x.strip()]
    for paragraph in paragraphs:
        if isinstance(paragraph, dict):
            paragraph = paragraph.get("text", "")
        if str(paragraph).strip():
            add_body_paragraph(doc, str(paragraph).strip())
    add_signature_noi_nhan(
        doc,
        noi_nhan_items=data.get("recipients") or ["- Như trên;"],
        chuc_vu=(data.get("signer_title") or "THỦ TRƯỞNG ĐƠN VỊ").upper(),
        nguoi_ky=data.get("signer_name") or "",
        phong_viet_tat=data.get("office_abbr") or "",
    )
    apply_page_numbering(doc, hide_first_page=True)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
