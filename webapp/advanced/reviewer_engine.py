from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Iterable

ABBR_RE = re.compile(r"\b([A-ZÀ-Ỹ][A-ZÀ-Ỹ0-9-]{1,9})\b")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
SPACE_PUNCT = re.compile(r"\s+([,.;:!?])")
MULTISPACE = re.compile(r"[ \t]{2,}")
NUMBER_CLAIM = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|mg|g|kg|mL|L|mmHg|mmol/L|µmol/L|pg/mL|ng/mL|CI|KTC|OR|RR|HR|AUC)\b", re.I)
EVIDENCE_WORDS = re.compile(r"\b(nghiên cứu|bằng chứng|khuyến cáo|guideline|meta-analysis|systematic review|tỷ lệ|nguy cơ|liên quan|dự báo|hiệu quả|an toàn)\b", re.I)
OVERCLAIM = {
    "chứng minh": "Nên dùng từ trung tính hơn như 'cho thấy' nếu thiết kế nghiên cứu không chứng minh quan hệ nhân quả.",
    "tốt nhất": "Tránh khẳng định 'tốt nhất' khi chưa có kiểm định so sánh trực tiếp.",
    "rất tốt": "Cần nêu tiêu chí định lượng thay vì nhận định định tính 'rất tốt'.",
    "hoàn toàn": "Tránh khẳng định tuyệt đối nếu chưa có bằng chứng phù hợp.",
}
MEDICAL_TERMS = {
    "TNF-alpha": "TNF-α",
    "tnf-alpha": "TNF-α",
    "p value": "p-value",
    "confidence interval": "khoảng tin cậy",
    "odds ratio": "tỷ số chênh",
}

@dataclass
class Suggestion:
    id: str
    category: str
    severity: str
    start: int
    end: int
    original: str
    replacement: str
    reason: str

    def json(self) -> dict:
        return asdict(self)


def _sid(category: str, start: int, original: str, replacement: str) -> str:
    raw = f"{category}:{start}:{original}:{replacement}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _suggest(out: list[Suggestion], category: str, severity: str, start: int, end: int,
             original: str, replacement: str, reason: str) -> None:
    out.append(Suggestion(_sid(category, start, original, replacement), category, severity,
                          start, end, original, replacement, reason))


def detect_claims(text: str) -> list[dict]:
    claims: list[dict] = []
    offset = 0
    for sentence in SENTENCE_RE.split(text):
        s = sentence.strip()
        if not s:
            offset += len(sentence) + 1
            continue
        idx = text.find(s, offset)
        if idx < 0:
            idx = offset
        needs = bool(NUMBER_CLAIM.search(s) or EVIDENCE_WORDS.search(s))
        has_citation = bool(re.search(r"\[(?:\d+|\d+[–-]\d+)\]|\([A-ZÀ-Ỹ][^)]*\d{4}[^)]*\)", s))
        if needs and not has_citation:
            claims.append({"start": idx, "end": idx + len(s), "text": s, "reason": "Câu chứa nhận định/số liệu có thể cần nguồn tham khảo."})
        offset = idx + len(s)
    return claims


def review_text(text: str, mode: str = "general") -> dict:
    text = text or ""
    suggestions: list[Suggestion] = []
    for m in SPACE_PUNCT.finditer(text):
        _suggest(suggestions, "grammar", "low", m.start(), m.end(), m.group(0), m.group(1), "Bỏ khoảng trắng trước dấu câu.")
    for m in MULTISPACE.finditer(text):
        _suggest(suggestions, "grammar", "low", m.start(), m.end(), m.group(0), " ", "Chuẩn hóa khoảng trắng.")
    for old, new in MEDICAL_TERMS.items():
        for m in re.finditer(re.escape(old), text, re.I):
            if m.group(0) != new:
                _suggest(suggestions, "terminology", "medium", m.start(), m.end(), m.group(0), new, "Chuẩn hóa thuật ngữ khoa học/y khoa.")
    if mode in {"academic", "medical", "research"}:
        low = text.lower()
        for word, reason in OVERCLAIM.items():
            pos = 0
            while True:
                i = low.find(word, pos)
                if i < 0:
                    break
                _suggest(suggestions, "academic", "high", i, i + len(word), text[i:i+len(word)], "", reason)
                pos = i + len(word)
    defined: set[str] = set()
    for m in re.finditer(r"([^()\n]{2,80})\s*\(([A-Z][A-Z0-9-]{1,9})\)", text):
        defined.add(m.group(2))
    abbr = sorted({m.group(1) for m in ABBR_RE.finditer(text) if len(m.group(1)) > 1})
    undefined = [x for x in abbr if x not in defined and x not in {"PDF", "DOCX", "DOI", "PMID", "AI"}]
    claims = detect_claims(text)
    score = max(0, 100 - len(suggestions) * 2 - len(claims) * 3 - len(undefined))
    return {
        "score": score,
        "mode": mode,
        "suggestions": [x.json() for x in suggestions],
        "claims_needing_citation": claims,
        "undefined_abbreviations": undefined,
        "stats": {"characters": len(text), "words": len(re.findall(r"\S+", text)), "suggestions": len(suggestions), "claims": len(claims)},
    }


def apply_suggestions(text: str, suggestions: Iterable[dict], accepted_ids: set[str] | None = None,
                      safe_only: bool = False) -> str:
    items = []
    for s in suggestions:
        if accepted_ids is not None and s.get("id") not in accepted_ids:
            continue
        if safe_only and s.get("category") not in {"grammar", "terminology"}:
            continue
        if s.get("replacement", None) is None:
            continue
        items.append(s)
    for s in sorted(items, key=lambda x: int(x["start"]), reverse=True):
        a, b = int(s["start"]), int(s["end"])
        if text[a:b] == s.get("original", ""):
            text = text[:a] + str(s.get("replacement", "")) + text[b:]
    return text


def track_changes(original: str, revised: str) -> list[dict]:
    matcher = SequenceMatcher(a=original, b=revised)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before, after = original[i1:i2], revised[j1:j2]
        out.append({
            "id": _sid("diff", i1, before, after), "type": tag,
            "start": i1, "end": i2, "original": before, "replacement": after,
        })
    return out
