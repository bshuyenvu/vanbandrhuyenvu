from __future__ import annotations

import io
import os
import unittest
import zipfile
from unittest.mock import patch

from webapp.advanced.citation_engine import format_vancouver
from webapp.advanced.docx_professional import build_professional_docx
from webapp.advanced.finalize import finalize_document
from webapp.advanced.github_store import GitHubStore, GitHubStoreError
from webapp.advanced.research_assistant import evidence_table
from webapp.advanced.reviewer_engine import apply_suggestions, review_text, track_changes
from webapp.advanced.template_builder import build_template, list_templates
from webapp.advanced.voice import normalize_vi_medical


class AdvancedV10Tests(unittest.TestCase):
    def test_v3_reviewer_and_safe_apply(self):
        text = "TNF-alpha  có liên quan tỷ lệ tử vong 20% . Kết quả chứng minh mô hình tốt nhất."
        review = review_text(text, "medical")
        self.assertLess(review["score"], 100)
        self.assertTrue(review["claims_needing_citation"])
        self.assertTrue(any(x["category"] == "terminology" for x in review["suggestions"]))
        self.assertTrue(any(x["category"] == "academic" for x in review["suggestions"]))
        fixed = apply_suggestions(text, review["suggestions"], safe_only=True)
        self.assertIn("TNF-α", fixed)
        self.assertNotIn("20% .", fixed)

    def test_track_changes(self):
        changes = track_changes("Viêm tụy cấp.", "Viêm tụy cấp nặng.")
        self.assertTrue(changes)
        self.assertTrue(any(x["replacement"] for x in changes))

    def test_v5_templates(self):
        ids = {x["id"] for x in list_templates()}
        self.assertTrue({"cong_van", "bao_cao", "benh_an", "luan_van", "bai_bao"}.issubset(ids))
        spec = build_template("luan_van", {"title": "Luận văn thử nghiệm"})
        self.assertTrue(spec["toc"])
        self.assertGreater(len(spec["blocks"]), 10)

    def test_v9_medical_voice_normalizer(self):
        out = normalize_vi_medical("t n f alpha tăng chấm a pa chi hai bằng bảy")
        self.assertIn("TNF-α", out)
        self.assertIn("APACHE II", out)
        self.assertIn(".", out)

    def test_citation_vancouver(self):
        ref = format_vancouver({
            "authors": ["Tran Huyen Vu", "Vo Minh Phuong"],
            "title": "Acute pancreatitis study", "journal": "Medical Journal",
            "year": "2026", "doi": "10.1000/example",
        }, 3)
        self.assertTrue(ref.startswith("3."))
        self.assertIn("doi:10.1000/example", ref)

    def test_v4_docx_professional_features(self):
        raw = build_professional_docx({
            "title": "Báo cáo thử nghiệm", "header": "Đơn vị", "footer": "Nội bộ",
            "toc": True, "page_numbers": True,
            "blocks": [
                {"type": "heading", "level": 1, "text": "Mở đầu"},
                {"type": "paragraph", "text": "Nội dung có chú thích{fn:Nguồn thử nghiệm}."},
                {"type": "table", "caption": "Bảng 1", "rows": [["A", "B"], ["1", "2"]]},
                {"type": "section_break"},
                {"type": "caption", "text": "Hình 1. Minh họa"},
            ],
        })
        self.assertTrue(raw.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = set(z.namelist())
            self.assertIn("word/footnotes.xml", names)
            self.assertIn("word/header1.xml", names)
            self.assertIn("word/footer1.xml", names)
            document = z.read("word/document.xml").decode("utf-8")
            self.assertIn("TOC", document)
            self.assertIn("footnoteReference", document)

    def test_v6_evidence_table(self):
        result = evidence_table([{
            "title": "Study A", "year": "2026", "journal": "J",
            "doi": "10.1/a", "authors": ["A Author"], "design": "cohort",
        }])
        self.assertEqual(len(result["rows"]), 1)
        self.assertIn("Study A", result["markdown"])

    def test_v10_finalize(self):
        result = finalize_document(
            "# Mở đầu\nTNF-alpha  tăng trong nghiên cứu 20% .",
            title="Tài liệu", mode="medical", auto_fix_safe=True,
        )
        self.assertIn("TNF-α", result["text"])
        self.assertTrue(result["docx"].startswith(b"PK"))
        self.assertIn("window.print", result["print_html"])
        self.assertTrue(result["changes"])

    def test_v7_production_rejects_public_docs_repo(self):
        with patch.dict(os.environ, {"VBHC_ENV": "production"}, clear=False):
            store = GitHubStore(token="test", repo="owner/public", branch="documents")
            with patch.object(store, "_request", return_value={"private": False}):
                with self.assertRaises(GitHubStoreError):
                    store._assert_repo_policy()

    def test_server_has_v3_to_v10_routes(self):
        os.environ.setdefault("VBHC_SESSION_SECRET", "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz")
        os.environ.setdefault("VBHC_DEV_DB", "/tmp/huyenvu-v10-routes.db")
        from webapp.server import app
        paths = {getattr(r, "path", "") for r in app.routes}
        required = {
            "/editor", "/api/v3/review", "/api/v4/docx", "/api/v5/templates",
            "/api/v6/research/search", "/api/v7/documents/save",
            "/api/v9/voice/normalize", "/api/v10/finalize",
        }
        self.assertTrue(required.issubset(paths), required - paths)


if __name__ == "__main__":
    unittest.main()
