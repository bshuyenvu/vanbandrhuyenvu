from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ai_router import AIError, AIRouter  # noqa: E402
from document_intake import compact_text, decode_payload, extract_protected_facts  # noqa: E402
from docx_export import build_government_reply  # noqa: E402
from party_docx import build_party_reply  # noqa: E402

AI = AIRouter()
STATIC_INDEX = HERE / "static" / "index.html"

ANALYZE_SYSTEM = """Bạn là trợ lý văn thư Việt Nam. Nhiệm vụ là phân tích văn bản đến để hỗ trợ cán bộ soạn văn bản trả lời.
Không được bịa số liệu, tên, chức vụ, số văn bản, thời hạn hoặc căn cứ pháp lý. Nếu không thấy rõ thì để chuỗi rỗng hoặc đưa vào missing_data.
Tách từng yêu cầu cần phản hồi thành một mục riêng. Kết quả phải là JSON object."""

DRAFT_SYSTEM = """Bạn là biên tập viên văn bản hành chính Việt Nam. Soạn bản DỰ THẢO phản hồi dựa duy nhất trên dữ liệu được cung cấp.
Không tự tạo số liệu, căn cứ pháp lý, số văn bản, tên người hoặc thời hạn. Chỗ thiếu dữ liệu phải dùng [CẦN BỔ SUNG: ...].
Mỗi yêu cầu trong văn bản đến phải được trả lời hoặc đánh dấu thiếu dữ liệu. Văn phong trang trọng, ngắn gọn, rõ trách nhiệm.
Kết quả phải là JSON object, không Markdown."""

REVIEW_SYSTEM = """Bạn là kiểm định viên văn bản hành chính. So sánh dự thảo với yêu cầu của văn bản đến.
Không sửa protected facts. Đánh giá mức độ trả lời từng yêu cầu, phát hiện mâu thuẫn số liệu/tên/ngày/căn cứ và phần còn thiếu.
Kết quả phải là JSON object."""


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def home(_: Request) -> Response:
    if STATIC_INDEX.is_file():
        return FileResponse(str(STATIC_INDEX), media_type="text/html; charset=utf-8")
    return _err("Thiếu webapp/static/index.html", 500)


async def health(_: Request) -> Response:
    return JSONResponse({"ok": True, "service": "vbhc-ai-reply", "ai": AI.status()})


async def ai_status(_: Request) -> Response:
    return JSONResponse({"ok": True, **AI.status()})


async def analyze(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Body phải là JSON")
    text = compact_text(str(body.get("text") or ""))
    file_b64 = str(body.get("file_base64") or "")
    filename = str(body.get("filename") or "document")
    mime_type = str(body.get("mime_type") or "application/octet-stream")
    payload = None
    if file_b64:
        try:
            payload = decode_payload(filename, mime_type, file_b64)
            if payload.text:
                text = compact_text(payload.text)
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    if not text and not payload:
        return _err("Cần nhập nội dung hoặc chọn file")

    facts = extract_protected_facts(text)
    prompt = f'''Phân tích văn bản đến sau và trả về đúng cấu trúc JSON:
{{
  "sender": "",
  "document_number": "",
  "document_date": "",
  "subject": "",
  "summary": "",
  "deadline": "",
  "priority": "normal|urgent|very_urgent",
  "requests": [{{"id":"R1","request":"","required_output":"","status":"unanswered"}}],
  "suggested_reply_type": "Công văn|Báo cáo|Tờ trình|Văn bản khác",
  "missing_data": [""],
  "legal_references_seen": [""],
  "warnings": [""]
}}

VĂN BẢN TRÍCH XUẤT:
{text[:30000] if text else '[File sẽ được AI đọc trực tiếp]'}
'''
    direct_file = None
    direct_mime = None
    if payload and mime_type in {"application/pdf", "image/png", "image/jpeg", "image/webp"}:
        direct_file = file_b64
        direct_mime = mime_type
    try:
        result = AI.generate_json(system=ANALYZE_SYSTEM, prompt=prompt, file_b64=direct_file, mime_type=direct_mime)
    except AIError as exc:
        return _err(str(exc), 503)

    analysis = result.data
    analysis["protected_facts"] = facts
    analysis["source_filename"] = filename
    return JSONResponse({"ok": True, "analysis": analysis, "provider": result.provider, "model": result.model})


async def draft_reply(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Body phải là JSON")
    analysis = body.get("analysis") or {}
    if not isinstance(analysis, dict) or not analysis:
        return _err("Thiếu analysis từ bước phân tích văn bản đến")
    context = str(body.get("context") or "").strip()
    standard = str(body.get("standard") or "government")
    org = body.get("organization") or {}
    protected = analysis.get("protected_facts") or []
    prompt = f'''Hãy soạn dự thảo trả lời.
CHUẨN: {standard}
THÔNG TIN CƠ QUAN: {json.dumps(org, ensure_ascii=False)}
PHÂN TÍCH VĂN BẢN ĐẾN: {json.dumps(analysis, ensure_ascii=False)}
DỮ LIỆU BỔ SUNG CỦA NGƯỜI DÙNG: {context or '[không có]'}
PROTECTED FACTS - KHÔNG TỰ Ý THAY ĐỔI: {json.dumps(protected, ensure_ascii=False)}

Trả JSON:
{{
  "reply_type":"Công văn",
  "subject":"",
  "recipient":"",
  "opening":"",
  "paragraphs":[""],
  "request_responses":[{{"request_id":"R1","status":"answered|partial|missing_data","response":"","needs":[""]}}],
  "missing_data":[""],
  "legal_citations_used":[""],
  "draft_text":"",
  "warnings":[""]
}}
'''
    try:
        result = AI.generate_json(system=DRAFT_SYSTEM, prompt=prompt)
    except AIError as exc:
        return _err(str(exc), 503)
    return JSONResponse({"ok": True, "draft": result.data, "provider": result.provider, "model": result.model})


async def review_reply(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Body phải là JSON")
    analysis = body.get("analysis") or {}
    draft = body.get("draft") or {}
    if not analysis or not draft:
        return _err("Thiếu analysis hoặc draft")
    prompt = f'''Đánh giá dự thảo so với văn bản đến.
ANALYSIS: {json.dumps(analysis, ensure_ascii=False)}
DRAFT: {json.dumps(draft, ensure_ascii=False)}
Trả JSON:
{{
  "score": 0,
  "ready_for_human_review": false,
  "coverage":[{{"request_id":"R1","status":"answered|partial|missing","note":""}}],
  "protected_fact_issues":[""],
  "consistency_issues":[""],
  "legal_citation_issues":[""],
  "missing_data":[""],
  "suggested_edits":[{{"find":"","replace":"","reason":""}}],
  "warnings":[""]
}}
'''
    try:
        result = AI.generate_json(system=REVIEW_SYSTEM, prompt=prompt)
    except AIError as exc:
        return _err(str(exc), 503)
    return JSONResponse({"ok": True, "review": result.data, "provider": result.provider, "model": result.model})


async def export_docx(request: Request) -> Response:
    try:
        body = await request.json()
        standard = str(body.get("standard") or "government").strip().lower()
        document_type = str(body.get("document_type") or body.get("reply_type") or "Công văn").strip().lower()
        if "công văn" not in document_type:
            return _err("V1 chỉ xuất DOCX đã kiểm định cho thể loại Công văn/phúc đáp. Báo cáo, Tờ trình và loại khác cần builder riêng.", 422)
        if standard == "government":
            raw = build_government_reply(body)
        elif standard == "party":
            raw = build_party_reply(body)
        else:
            return _err(f"Chuẩn văn bản không hỗ trợ: {standard}", 422)
    except Exception as exc:
        return _err(f"Không tạo được DOCX: {exc}", 500)
    filename = str(body.get("download_name") or "du-thao-phuc-dap.docx")
    if not filename.lower().endswith(".docx"):
        filename += ".docx"
    safe_name = filename.encode("ascii", "ignore").decode() or "reply.docx"
    headers = {"Content-Disposition": f'attachment; filename="{safe_name}"'}
    return Response(raw, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)


routes = [
    Route("/", home, methods=["GET"]),
    Route("/healthz", health, methods=["GET"]),
    Route("/api/ai/status", ai_status, methods=["GET"]),
    Route("/api/incoming/analyze", analyze, methods=["POST"]),
    Route("/api/reply/draft", draft_reply, methods=["POST"]),
    Route("/api/reply/review", review_reply, methods=["POST"]),
    Route("/api/reply/export/docx", export_docx, methods=["POST"]),
]
app = Starlette(routes=routes)


def main() -> None:
    parser = argparse.ArgumentParser(description="VBHC AI Incoming/Reply web service")
    parser.add_argument("--host", default=os.getenv("VBHC_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("VBHC_WEB_PORT", "8767")))
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
