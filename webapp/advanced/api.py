from __future__ import annotations

import base64
import json
import re
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .citation_engine import search_crossref, search_pubmed, validate_doi, validate_pmid
from .docx_professional import build_professional_docx
from .finalize import finalize_document
from .github_store import GitHubStore, GitHubStoreError
from .research_assistant import evidence_table, search_evidence
from .reviewer_engine import apply_suggestions, review_text, track_changes
from .template_builder import build_template, list_templates
from .voice import normalize_vi_medical


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(message: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": message}, status)


def _user(request: Request):
    from saas.api import current_user
    user = current_user(request)
    allow = os.getenv("VBHC_ALLOW_ANON_ADVANCED", "false").lower() in {"1","true","yes"}
    return user if user else ({"id":"anonymous","email":"anonymous"} if allow else None)


async def capabilities(_: Request):
    return _json({"ok": True, "versions": {
        "v3": ["reviewer", "track_changes", "citation"],
        "v4": ["header_footer", "sections", "page_numbers", "toc", "tables", "captions", "footnotes"],
        "v5": ["cong_van", "bao_cao", "benh_an", "luan_van", "bai_bao"],
        "v6": ["pubmed", "crossref", "evidence_table"],
        "v7": ["github_save", "comment", "review", "review_branch", "pull_request"],
        "v8": ["pwa", "offline_autosave", "offline_queue", "github_sync"],
        "v9": ["vi-VN_web_speech", "medical_normalizer"],
        "v10": ["audit", "safe_fix", "citation_review", "docx", "print_pdf"],
    }})


async def reviewer(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json(); text = str(body.get("text") or ""); mode = str(body.get("mode") or "general")
    return _json({"ok": True, "review": review_text(text, mode)})


async def apply_review(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json(); text = str(body.get("text") or ""); suggestions = body.get("suggestions") or []
    ids = set(body.get("accepted_ids") or []) if body.get("accepted_ids") is not None else None
    revised = apply_suggestions(text, suggestions, accepted_ids=ids, safe_only=bool(body.get("safe_only", False)))
    return _json({"ok": True, "text": revised, "changes": track_changes(text, revised)})


async def citations_search(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json(); query = str(body.get("query") or "").strip(); source = str(body.get("source") or "pubmed"); limit = int(body.get("limit") or 5)
    if not query: return _error("Thiếu query")
    try: results = search_pubmed(query, limit) if source == "pubmed" else search_crossref(query, limit)
    except Exception as exc: return _error(f"Không truy vấn được {source}: {exc}", 502)
    return _json({"ok": True, "source": source, "results": results})


async def citations_verify(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json(); doi = str(body.get("doi") or ""); pmid = str(body.get("pmid") or "")
    return _json({"ok": True, "doi": validate_doi(doi) if doi else None, "pmid": validate_pmid(pmid) if pmid else None})


async def templates(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    return _json({"ok": True, "templates": list_templates()})


async def template_build(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json()
    try: spec = build_template(str(body.get("template_id") or ""), body.get("data") or {})
    except ValueError as exc: return _error(str(exc), 422)
    return _json({"ok": True, "spec": spec})


async def docx_export(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json()
    try: raw = build_professional_docx(body.get("spec") or body)
    except Exception as exc: return _error(f"Không tạo được DOCX: {exc}", 500)
    return Response(raw, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": 'attachment; filename="huyen-vu-document.docx"'})


async def research_search(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json(); query = str(body.get("query") or "").strip()
    if not query: return _error("Thiếu query")
    result = search_evidence(query, int(body.get("limit") or 5), tuple(body.get("sources") or ["pubmed","crossref"]))
    return _json({"ok": True, **result})


async def research_table(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json(); return _json({"ok": True, **evidence_table(body.get("items") or [])})


async def document_save(request: Request):
    user = _user(request)
    if not user: return _error("Chưa đăng nhập", 401)
    body = await request.json(); document_id = str(body.get("document_id") or "untitled"); text = str(body.get("text") or "")
    try: result = GitHubStore().save_document(str(user["id"]), document_id, text, body.get("metadata") or {})
    except GitHubStoreError as exc: return _error(str(exc), 503)
    return _json({"ok": True, "saved": result})


async def collaboration_pr(request: Request):
    user = _user(request)
    if not user: return _error("Chưa đăng nhập", 401)
    body = await request.json(); name = str(body.get("name") or "document-review")
    try:
        store = GitHubStore(); branch = store.create_review_branch(name, str(body.get("base") or "main"))
        path = str(body.get("path") or f"reviews/{name}.md")
        store.put(path, str(body.get("text") or ""), f"review: {name}", branch=branch)
        pr = store.open_pr(str(body.get("title") or f"Review: {name}"), branch, str(body.get("base") or "main"), str(body.get("body") or "AI Word collaboration review"))
    except GitHubStoreError as exc: return _error(str(exc), 503)
    return _json({"ok": True, "branch": branch, "pull_request": {"number": pr.get("number"), "url": pr.get("html_url")}})


async def collaboration_comment(request: Request):
    user = _user(request)
    if not user: return _error("Chưa đăng nhập", 401)
    body = await request.json(); number = int(body.get("pr_number") or 0); comment = str(body.get("comment") or "").strip()
    if number < 1 or not comment: return _error("Thiếu pr_number hoặc comment")
    try: result = GitHubStore().comment_pr(number, comment)
    except GitHubStoreError as exc: return _error(str(exc), 503)
    return _json({"ok": True, "comment": {"id": result.get("id"), "url": result.get("html_url")}})


async def collaboration_review(request: Request):
    user = _user(request)
    if not user: return _error("Chưa đăng nhập", 401)
    body = await request.json(); number = int(body.get("pr_number") or 0); note = str(body.get("body") or "").strip(); event = str(body.get("event") or "COMMENT")
    if number < 1: return _error("Thiếu pr_number")
    try: result = GitHubStore().review_pr(number, note, event)
    except GitHubStoreError as exc: return _error(str(exc), 503)
    return _json({"ok": True, "review": {"id": result.get("id"), "state": result.get("state"), "url": result.get("html_url")}})


async def voice_normalize(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json(); return _json({"ok": True, "text": normalize_vi_medical(str(body.get("text") or ""))})


def _verify_citations(text: str) -> dict:
    dois = list(dict.fromkeys(re.findall(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.I)))[:20]
    pmids = list(dict.fromkeys(re.findall(r"PMID\s*:?\s*(\d{5,9})", text, re.I)))[:20]
    doi_checks = [validate_doi(x.rstrip(".,;)]")) for x in dois]
    pmid_checks = [validate_pmid(x) for x in pmids]
    valid = sum(1 for x in doi_checks + pmid_checks if x.get("valid"))
    return {"doi": doi_checks, "pmid": pmid_checks, "checked": len(doi_checks) + len(pmid_checks), "valid": valid}


async def finalize(request: Request):
    if not _user(request): return _error("Chưa đăng nhập", 401)
    body = await request.json(); text = str(body.get("text") or "")
    citation_checks = _verify_citations(text) if bool(body.get("verify_citations", True)) else {"doi": [], "pmid": [], "checked": 0, "valid": 0}
    result = finalize_document(text, title=str(body.get("title") or "Văn bản"), mode=str(body.get("mode") or "general"), auto_fix_safe=bool(body.get("auto_fix_safe", True)), spec=body.get("spec"))
    return _json({"ok": True, "review": result["review"], "citation_checks": citation_checks, "text": result["text"], "changes": result["changes"], "docx_base64": base64.b64encode(result["docx"]).decode("ascii"), "print_html": result["print_html"]})


def routes():
    return [
        Route("/api/v3/capabilities", capabilities, methods=["GET"]),
        Route("/api/v3/review", reviewer, methods=["POST"]), Route("/api/v3/review/apply", apply_review, methods=["POST"]),
        Route("/api/v3/citations/search", citations_search, methods=["POST"]), Route("/api/v3/citations/verify", citations_verify, methods=["POST"]),
        Route("/api/v5/templates", templates, methods=["GET"]), Route("/api/v5/templates/build", template_build, methods=["POST"]),
        Route("/api/v4/docx", docx_export, methods=["POST"]), Route("/api/v6/research/search", research_search, methods=["POST"]),
        Route("/api/v6/research/table", research_table, methods=["POST"]), Route("/api/v7/documents/save", document_save, methods=["POST"]),
        Route("/api/v7/collaboration/pr", collaboration_pr, methods=["POST"]),
        Route("/api/v7/collaboration/comment", collaboration_comment, methods=["POST"]),
        Route("/api/v7/collaboration/review", collaboration_review, methods=["POST"]),
        Route("/api/v9/voice/normalize", voice_normalize, methods=["POST"]),
        Route("/api/v10/finalize", finalize, methods=["POST"]),
    ]
