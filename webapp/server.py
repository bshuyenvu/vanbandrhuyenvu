from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ai_router import AIError, AIRouter  # noqa: E402
from document_intake import compact_text, decode_payload, extract_protected_facts  # noqa: E402
from docx_export import build_government_reply  # noqa: E402
from party_docx import build_party_reply  # noqa: E402
from saas.admin_api import routes as admin_routes  # noqa: E402
from saas.api import current_user, org_role, platform_admin, routes as saas_routes  # noqa: E402
from saas.catalog import PROJECT_NAME  # noqa: E402
from saas.export_api import routes as export_routes  # noqa: E402
from saas.model_manager import choose_model  # noqa: E402
from saas.store import active_plan, all_rows, charge_ai_usage, ensure_credit, one, wallet_for  # noqa: E402
from saas.usage import ensure_usage_quota  # noqa: E402
from saas.usage_api import routes as usage_routes  # noqa: E402

AI = AIRouter()
STATIC_INDEX = HERE / "static" / "index.html"
STATIC_CONSOLE = HERE / "static" / "console.html"

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


class AccessError(RuntimeError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _configured_registry() -> dict[str, dict[str, Any]]:
    rows = all_rows("SELECT * FROM ai_models WHERE enabled=1 ORDER BY id")
    registry: dict[str, dict[str, Any]] = {}
    for row in rows:
        provider = str(row.get("provider") or "").lower()
        if provider in {"google", "gemini"} and not AI.gemini_key:
            continue
        if provider == "openai" and not AI.openai_key:
            continue
        registry[row["id"]] = {**row, "enabled": bool(row.get("enabled"))}
    return registry


def _ai_context(request: Request, body: dict[str, Any], task_type: str) -> dict[str, Any]:
    user = current_user(request)
    if not user:
        if os.getenv("VBHC_ALLOW_ANON_AI", "false").lower() in {"1", "true", "yes"}:
            return {"anonymous": True, "choice": None, "organization_id": None, "department_id": None}
        raise AccessError("Vui lòng đăng nhập để sử dụng AI", 401)

    org_id = str(body.get("organization_id") or "").strip() or None
    department_id = str(body.get("department_id") or "").strip() or None
    if org_id and not platform_admin(user) and not org_role(user["id"], org_id):
        raise AccessError("Bạn không thuộc cơ quan/đơn vị này", 403)

    data_policy = "internal"
    if org_id:
        org = one("SELECT data_policy FROM organizations WHERE id=?", (org_id,))
        if not org:
            raise AccessError("Không tìm thấy cơ quan/đơn vị", 404)
        data_policy = str(org.get("data_policy") or "internal")
        if data_policy == "confidential" and os.getenv("VBHC_ALLOW_CONFIDENTIAL_EXTERNAL_AI", "false").lower() not in {"1", "true", "yes"}:
            data_policy = "restricted"

    billing_scope = str(body.get("billing_scope") or ("organization" if org_id else "personal"))
    if billing_scope == "organization" and org_id:
        plan_id = active_plan("organization", org_id)
        wallet = wallet_for(user_id=user["id"], organization_id=org_id)
        quota_org_id = org_id
    else:
        plan_id = active_plan("user", user["id"])
        wallet = wallet_for(user_id=user["id"])
        quota_org_id = None
    ensure_credit(wallet, int(os.getenv("VBHC_MIN_AI_CREDIT", "50")))
    ensure_usage_quota(user_id=user["id"], plan_id=plan_id, wallet=wallet, organization_id=quota_org_id)

    choice = choose_model(
        task_type=task_type,
        plan_id=plan_id,
        data_policy=data_policy,
        requested_tier=str(body.get("model_tier") or "").strip() or None,
        registry=_configured_registry(),
    )
    return {
        "anonymous": False,
        "user": user,
        "organization_id": org_id,
        "department_id": department_id,
        "billing_scope": billing_scope,
        "plan_id": plan_id,
        "wallet": wallet,
        "data_policy": data_policy,
        "choice": choice,
    }


def _run_ai(*, ctx: dict[str, Any], task_type: str, system: str, prompt: str,
            file_b64: str | None = None, mime_type: str | None = None):
    choice = ctx.get("choice")
    if choice:
        result = AI.generate_json(
            system=system,
            prompt=prompt,
            file_b64=file_b64,
            mime_type=mime_type,
            preferred_provider=choice.provider,
            preferred_model=choice.model_name,
        )
    else:
        result = AI.generate_json(system=system, prompt=prompt, file_b64=file_b64, mime_type=mime_type)

    billing = None
    if not ctx.get("anonymous"):
        user = ctx["user"]
        billing = charge_ai_usage(
            user_id=user["id"],
            organization_id=ctx.get("organization_id"),
            department_id=ctx.get("department_id"),
            provider=result.provider,
            model_name=result.model,
            task_type=task_type,
            usage=result.usage,
        )
    return result, billing


async def home(_: Request) -> Response:
    if STATIC_CONSOLE.is_file():
        html = STATIC_CONSOLE.read_text(encoding="utf-8")
        state_patch = """<script>
(function(){
 const el=document.getElementById('orgSelect');
 if(el){
   el.addEventListener('change',function(){
     if(this.value)localStorage.setItem('hv_vbai_active_org',this.value);
     else localStorage.removeItem('hv_vbai_active_org');
   });
   setTimeout(function(){
     const saved=localStorage.getItem('hv_vbai_active_org');
     if(saved && el.querySelector('option[value="'+saved+'"]')){
       el.value=saved; el.dispatchEvent(new Event('change'));
     }
   },350);
 }
 const plans=document.querySelector('[data-view="plans"]');
 if(plans && !document.getElementById('usageNav')){
   const b=document.createElement('button'); b.id='usageNav'; b.className='nav';
   b.innerHTML='◉ Lượt & Token của tôi'; b.onclick=function(){location.href='/usage'};
   plans.insertAdjacentElement('afterend',b);
 }
 const credit=document.getElementById('creditPill');
 if(credit){credit.style.cursor='pointer';credit.title='Xem lượt, token và AI Credit';credit.onclick=function(){location.href='/usage'}}
})();
</script>"""
        return HTMLResponse(html.replace("</body>", state_patch + "</body>"))
    return _err("Thiếu webapp/static/console.html", 500)


async def reply_workbench(_: Request) -> Response:
    if not STATIC_INDEX.is_file():
        return _err("Thiếu webapp/static/index.html", 500)
    html = STATIC_INDEX.read_text(encoding="utf-8")
    bootstrap = """<script>
(function(){
 const token=localStorage.getItem('hv_vbai_token');
 if(!token){location.replace('/');return;}
 const nativeFetch=window.fetch.bind(window);
 window.fetch=function(url,opts){
   opts=opts||{}; opts.headers=Object.assign({},opts.headers||{},{Authorization:'Bearer '+token});
   try{
     if(typeof opts.body==='string' && String(url).startsWith('/api/')){
       const data=JSON.parse(opts.body); const org=localStorage.getItem('hv_vbai_active_org');
       if(org && !data.organization_id)data.organization_id=org;
       if(org && !data.billing_scope)data.billing_scope='organization';
       opts.body=JSON.stringify(data);
     }
   }catch(e){}
   return nativeFetch(url,opts);
 };
})();
</script>"""
    return HTMLResponse(html.replace("</head>", bootstrap + "</head>"))


async def health(_: Request) -> Response:
    return JSONResponse({"ok": True, "service": "huyen-vu-van-ban-ai", "project": PROJECT_NAME, "version": "2.1-usage", "ai": AI.status()})


async def ai_status(_: Request) -> Response:
    return JSONResponse({"ok": True, **AI.status()})


async def analyze(request: Request) -> Response:
    try:
        body = await request.json()
        ctx = _ai_context(request, body, "extract")
    except AccessError as exc:
        return _err(str(exc), exc.status)
    except (ValueError, RuntimeError) as exc:
        return _err(str(exc), 402)
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
  "sender": "", "document_number": "", "document_date": "", "subject": "",
  "summary": "", "deadline": "", "priority": "normal|urgent|very_urgent",
  "requests": [{{"id":"R1","request":"","required_output":"","status":"unanswered"}}],
  "suggested_reply_type": "Công văn|Báo cáo|Tờ trình|Văn bản khác",
  "missing_data": [""], "legal_references_seen": [""], "warnings": [""]
}}

VĂN BẢN TRÍCH XUẤT:
{text[:30000] if text else '[File sẽ được AI đọc trực tiếp]'}
'''
    direct_file = file_b64 if payload and mime_type in {"application/pdf", "image/png", "image/jpeg", "image/webp"} else None
    direct_mime = mime_type if direct_file else None
    try:
        result, billing = _run_ai(ctx=ctx,task_type="extract",system=ANALYZE_SYSTEM,prompt=prompt,file_b64=direct_file,mime_type=direct_mime)
    except (AIError, ValueError) as exc:
        return _err(str(exc), 503 if isinstance(exc, AIError) else 402)

    analysis = result.data
    analysis["protected_facts"] = facts
    analysis["source_filename"] = filename
    return JSONResponse({"ok": True, "analysis": analysis, "provider": result.provider, "model": result.model, "usage": result.usage, "billing": billing})


async def draft_reply(request: Request) -> Response:
    try:
        body = await request.json()
        ctx = _ai_context(request, body, "draft")
    except AccessError as exc:
        return _err(str(exc), exc.status)
    except (ValueError, RuntimeError) as exc:
        return _err(str(exc), 402)
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
Trả JSON: {{"reply_type":"Công văn","subject":"","recipient":"","opening":"","paragraphs":[""],"request_responses":[{{"request_id":"R1","status":"answered|partial|missing_data","response":"","needs":[""]}}],"missing_data":[""],"legal_citations_used":[""],"draft_text":"","warnings":[""]}}
'''
    try:
        result, billing = _run_ai(ctx=ctx,task_type="draft",system=DRAFT_SYSTEM,prompt=prompt)
    except (AIError, ValueError) as exc:
        return _err(str(exc), 503 if isinstance(exc, AIError) else 402)
    return JSONResponse({"ok": True, "draft": result.data, "provider": result.provider, "model": result.model, "usage": result.usage, "billing": billing})


async def review_reply(request: Request) -> Response:
    try:
        body = await request.json()
        ctx = _ai_context(request, body, "review")
    except AccessError as exc:
        return _err(str(exc), exc.status)
    except (ValueError, RuntimeError) as exc:
        return _err(str(exc), 402)
    except Exception:
        return _err("Body phải là JSON")
    analysis = body.get("analysis") or {}
    draft = body.get("draft") or {}
    if not analysis or not draft:
        return _err("Thiếu analysis hoặc draft")
    prompt = f'''Đánh giá dự thảo so với văn bản đến.
ANALYSIS: {json.dumps(analysis, ensure_ascii=False)}
DRAFT: {json.dumps(draft, ensure_ascii=False)}
Trả JSON: {{"score":0,"ready_for_human_review":false,"coverage":[{{"request_id":"R1","status":"answered|partial|missing","note":""}}],"protected_fact_issues":[""],"consistency_issues":[""],"legal_citation_issues":[""],"missing_data":[""],"suggested_edits":[{{"find":"","replace":"","reason":""}}],"warnings":[""]}}
'''
    try:
        result, billing = _run_ai(ctx=ctx,task_type="review",system=REVIEW_SYSTEM,prompt=prompt)
    except (AIError, ValueError) as exc:
        return _err(str(exc), 503 if isinstance(exc, AIError) else 402)
    return JSONResponse({"ok": True, "review": result.data, "provider": result.provider, "model": result.model, "usage": result.usage, "billing": billing})


async def export_docx(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _err("Body phải là JSON")
    user = current_user(request)
    if not user and os.getenv("VBHC_ALLOW_ANON_AI", "false").lower() not in {"1","true","yes"}:
        return _err("Vui lòng đăng nhập", 401)
    org_id = str(body.get("organization_id") or "").strip()
    if user and org_id and not platform_admin(user) and not org_role(user["id"],org_id):
        return _err("Bạn không thuộc cơ quan/đơn vị này", 403)
    try:
        standard = str(body.get("standard") or "government").strip().lower()
        document_type = str(body.get("document_type") or body.get("reply_type") or "Công văn").strip().lower()
        if "công văn" not in document_type:
            return _err("Endpoint tương thích này chỉ xuất Công văn/phúc đáp. Dùng /api/v2/export/docx cho các thể loại V2.", 422)
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
    return Response(raw, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f'attachment; filename="{safe_name}"'})


routes = [
    Route("/", home, methods=["GET"]),
    Route("/reply", reply_workbench, methods=["GET"]),
    Route("/healthz", health, methods=["GET"]),
    Route("/api/ai/status", ai_status, methods=["GET"]),
    Route("/api/incoming/analyze", analyze, methods=["POST"]),
    Route("/api/reply/draft", draft_reply, methods=["POST"]),
    Route("/api/reply/review", review_reply, methods=["POST"]),
    Route("/api/reply/export/docx", export_docx, methods=["POST"]),
]
routes.extend(saas_routes())
routes.extend(admin_routes())
routes.extend(export_routes())
routes.extend(usage_routes())
app = Starlette(routes=routes)


def main() -> None:
    parser = argparse.ArgumentParser(description="Huyền Vũ Văn Bản AI V2")
    parser.add_argument("--host", default=os.getenv("VBHC_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("VBHC_WEB_PORT", "8767")))
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
