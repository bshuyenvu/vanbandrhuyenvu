from __future__ import annotations

from .citation_engine import search_crossref, search_pubmed, format_vancouver


def search_evidence(query: str, limit: int = 5, sources: tuple[str,...] = ("pubmed","crossref")) -> dict:
    out: list[dict] = []; errors: list[str] = []
    if "pubmed" in sources:
        try: out.extend(search_pubmed(query, limit))
        except Exception as exc: errors.append(f"PubMed: {exc}")
    if "crossref" in sources:
        try: out.extend(search_crossref(query, limit))
        except Exception as exc: errors.append(f"Crossref: {exc}")
    seen = set(); rows = []
    for item in out:
        key = str(item.get("doi") or item.get("pmid") or item.get("title") or "").lower()
        if not key or key in seen: continue
        seen.add(key); rows.append(item)
    return {"query": query, "results": rows, "errors": errors}


def evidence_table(items: list[dict]) -> dict:
    rows = []
    for i, x in enumerate(items, 1):
        rows.append({
            "no": i, "title": x.get("title", ""), "year": x.get("year", ""), "journal": x.get("journal", ""),
            "design": x.get("design", ""), "population": x.get("population", ""), "outcome": x.get("outcome", ""),
            "effect": x.get("effect", ""), "doi": x.get("doi", ""), "pmid": x.get("pmid", ""),
            "reference": format_vancouver(x, i),
        })
    markdown = "| # | Nghiên cứu | Năm | Thiết kế | Quần thể | Kết cục | Hiệu quả | DOI/PMID |\n|---:|---|---:|---|---|---|---|---|\n"
    for r in rows:
        ident = r["doi"] or r["pmid"]
        markdown += f'| {r["no"]} | {r["title"]} | {r["year"]} | {r["design"]} | {r["population"]} | {r["outcome"]} | {r["effect"]} | {ident} |\n'
    return {"rows": rows, "markdown": markdown}
