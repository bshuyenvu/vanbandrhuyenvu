from __future__ import annotations

from copy import deepcopy

TEMPLATES = {
    "cong_van": {"name":"Công văn", "title":"CÔNG VĂN", "sections":["Kính gửi", "Nội dung", "Nơi nhận"]},
    "bao_cao": {"name":"Báo cáo", "title":"BÁO CÁO", "sections":["I. Tình hình chung", "II. Kết quả", "III. Khó khăn", "IV. Kiến nghị"]},
    "benh_an": {"name":"Bệnh án", "title":"BỆNH ÁN", "sections":["I. Hành chính", "II. Lý do vào viện", "III. Bệnh sử", "IV. Tiền sử", "V. Khám", "VI. Cận lâm sàng", "VII. Chẩn đoán", "VIII. Điều trị"]},
    "luan_van": {"name":"Luận văn", "title":"LUẬN VĂN", "sections":["ĐẶT VẤN ĐỀ", "CHƯƠNG 1. TỔNG QUAN", "CHƯƠNG 2. ĐỐI TƯỢNG VÀ PHƯƠNG PHÁP", "CHƯƠNG 3. KẾT QUẢ", "CHƯƠNG 4. BÀN LUẬN", "KẾT LUẬN", "KIẾN NGHỊ", "TÀI LIỆU THAM KHẢO"]},
    "bai_bao": {"name":"Bài báo khoa học", "title":"BÀI BÁO KHOA HỌC", "sections":["Tóm tắt", "Đặt vấn đề", "Đối tượng và phương pháp", "Kết quả", "Bàn luận", "Kết luận", "Tài liệu tham khảo"]},
}

def list_templates() -> list[dict]:
    return [{"id": key, "name": value["name"], "sections": value["sections"]} for key, value in TEMPLATES.items()]

def build_template(template_id: str, data: dict | None = None) -> dict:
    if template_id not in TEMPLATES:
        raise ValueError("Template không hỗ trợ")
    data = data or {}; t = deepcopy(TEMPLATES[template_id]); blocks = []
    for section in t["sections"]:
        blocks.append({"type":"heading", "level": 1, "text": section})
        blocks.append({"type":"paragraph", "text": str(data.get(section, data.get("body", "")))})
    return {
        "template_id": template_id,
        "title": str(data.get("title") or t["title"]),
        "header": str(data.get("header") or ""),
        "footer": str(data.get("footer") or ""),
        "toc": template_id in {"luan_van", "bai_bao"},
        "page_numbers": True,
        "blocks": blocks,
    }
