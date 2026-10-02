from __future__ import annotations

import html
from .docx_professional import build_professional_docx
from .reviewer_engine import apply_suggestions, review_text, track_changes


def text_to_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("### "):
            blocks.append({"type":"heading","level":3,"text":line[4:]})
        elif line.startswith("## "):
            blocks.append({"type":"heading","level":2,"text":line[3:]})
        elif line.startswith("# "):
            blocks.append({"type":"heading","level":1,"text":line[2:]})
        else:
            blocks.append({"type":"paragraph","text":line})
    return blocks


def print_html(title: str, text: str) -> str:
    paras = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            paras.append("<p>&nbsp;</p>")
        elif line.startswith("# "):
            paras.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            paras.append(f"<h2>{html.escape(line[3:])}</h2>")
        else:
            paras.append(f"<p>{html.escape(line)}</p>")
    return "<!doctype html><html><head><meta charset='utf-8'><title>%s</title><style>@page{size:A4;margin:20mm 20mm 20mm 30mm}body{font-family:'Times New Roman',serif;font-size:13pt;line-height:1.35}h1,h2{text-align:center}p{text-align:justify;text-indent:1cm}</style></head><body>%s<script>window.onload=()=>window.print()</script></body></html>" % (html.escape(title), "".join(paras))


def finalize_document(text: str, *, title: str = "Văn bản", mode: str = "general",
                      auto_fix_safe: bool = True, spec: dict | None = None) -> dict:
    review = review_text(text, mode)
    revised = apply_suggestions(text, review["suggestions"], safe_only=True) if auto_fix_safe else text
    changes = track_changes(text, revised)
    doc_spec = dict(spec or {})
    doc_spec.setdefault("title", title)
    doc_spec.setdefault("toc", mode in {"academic","medical","research"})
    doc_spec.setdefault("page_numbers", True)
    doc_spec.setdefault("blocks", text_to_blocks(revised))
    raw = build_professional_docx(doc_spec)
    return {
        "review": review,
        "text": revised,
        "changes": changes,
        "docx": raw,
        "print_html": print_html(title, revised),
    }
