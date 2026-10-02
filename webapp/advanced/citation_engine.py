from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

UA = "HuyenVuVanBanAI/3.0 (citation verifier)"


def _get_json(url: str, timeout: int = 12) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def search_crossref(query: str, limit: int = 5) -> list[dict]:
    q = urllib.parse.quote(query.strip())
    data = _get_json(f"https://api.crossref.org/works?query.bibliographic={q}&rows={max(1,min(limit,20))}")
    rows = []
    for x in data.get("message", {}).get("items", []):
        authors = [" ".join(filter(None, [a.get("given"), a.get("family")])) for a in x.get("author", [])]
        rows.append({
            "source": "crossref", "title": (x.get("title") or [""])[0], "authors": authors,
            "year": ((x.get("published-print") or x.get("published-online") or {}).get("date-parts") or [[None]])[0][0],
            "journal": (x.get("container-title") or [""])[0], "doi": x.get("DOI", ""), "url": x.get("URL", ""),
        })
    return rows


def validate_doi(doi: str) -> dict:
    doi = doi.strip().replace("https://doi.org/", "").replace("http://doi.org/", "")
    if not doi:
        return {"valid": False, "error": "empty DOI"}
    try:
        data = _get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""))
        item = data.get("message", {})
        return {"valid": True, "doi": item.get("DOI", doi), "title": (item.get("title") or [""])[0], "type": item.get("type", "")}
    except Exception as exc:
        return {"valid": False, "doi": doi, "error": str(exc)}


def search_pubmed(query: str, limit: int = 5) -> list[dict]:
    q = urllib.parse.quote(query.strip())
    lim = max(1, min(limit, 20))
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    ids = _get_json(f"{base}/esearch.fcgi?db=pubmed&retmode=json&retmax={lim}&term={q}").get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    summary = _get_json(f"{base}/esummary.fcgi?db=pubmed&retmode=json&id={','.join(ids)}").get("result", {})
    out = []
    for pmid in ids:
        x = summary.get(pmid, {})
        articleids = {a.get("idtype"): a.get("value") for a in x.get("articleids", [])}
        out.append({
            "source": "pubmed", "pmid": pmid, "title": x.get("title", ""),
            "authors": [a.get("name", "") for a in x.get("authors", [])], "journal": x.get("fulljournalname", x.get("source", "")),
            "year": str(x.get("pubdate", ""))[:4], "doi": articleids.get("doi", ""),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return out


def validate_pmid(pmid: str) -> dict:
    pmid = "".join(ch for ch in pmid if ch.isdigit())
    if not pmid:
        return {"valid": False, "error": "empty PMID"}
    try:
        rows = search_pubmed(f"{pmid}[pmid]", 1)
        ok = bool(rows and rows[0].get("pmid") == pmid)
        return {"valid": ok, **(rows[0] if ok else {"pmid": pmid})}
    except Exception as exc:
        return {"valid": False, "pmid": pmid, "error": str(exc)}


def format_vancouver(item: dict, number: int | None = None) -> str:
    authors = item.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    author_text = ", ".join(authors[:6]) + (", et al." if len(authors) > 6 else "")
    title = str(item.get("title") or "").rstrip(".")
    journal = str(item.get("journal") or "").rstrip(".")
    year = str(item.get("year") or "")
    doi = str(item.get("doi") or "")
    ref = f"{author_text}. {title}. {journal}. {year}.".strip()
    if doi:
        ref += f" doi:{doi}."
    return f"{number}. {ref}" if number else ref
