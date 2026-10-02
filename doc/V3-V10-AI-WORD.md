# Huyền Vũ AI Word — V3 đến V10

## Nguyên tắc

- GitHub là source of truth cho source code, tài liệu, metadata tài liệu, lịch sử phiên bản, review branch và pull request.
- Production phải cấu hình `VBHC_GITHUB_DOCS_REPO` tới một repository **private**.
- `VBHC_GITHUB_TOKEN` và khóa AI chỉ tồn tại ở secret runtime, không commit vào Git.
- PWA chỉ giữ draft và hàng chờ tạm trên thiết bị khi offline; khi có mạng sẽ đồng bộ GitHub.
- PubMed/Crossref dùng API công khai; Citation Engine xác minh DOI/PMID thay vì tự tạo mã tham chiếu.
- PDF được tạo bằng Print-to-PDF phía trình duyệt, không mở thêm kho lưu trữ ngoài GitHub.

## V3 — Reviewer + Citation

- `POST /api/v3/review`: chính tả, khoảng trắng, thuật ngữ, overclaim, viết tắt, claim cần nguồn.
- `POST /api/v3/review/apply`: Accept từng thay đổi hoặc áp dụng nhóm sửa an toàn.
- `POST /api/v3/citations/search`: tìm PubMed/Crossref.
- `POST /api/v3/citations/verify`: xác minh DOI/PMID.

## V4 — DOCX Professional Engine

Hỗ trợ header/footer, section break, page number, TOC field, table, caption, footnote, margin và font.

Endpoint: `POST /api/v4/docx`.

## V5 — Template Builder

Mẫu dựng sẵn: công văn, báo cáo, bệnh án, luận văn, bài báo khoa học.

Endpoints: `GET /api/v5/templates`, `POST /api/v5/templates/build`.

## V6 — Research Assistant

PubMed/Crossref → khử trùng lặp → Evidence Table → chèn vào tài liệu.

Endpoints: `POST /api/v6/research/search`, `POST /api/v6/research/table`.

## V7 — Collaboration GitHub-native

- Lưu tài liệu và metadata trực tiếp vào GitHub.
- Tạo review branch và pull request.
- Comment, approve hoặc request changes trên PR.

Endpoints nằm dưới `/api/v7/`.

## V8 — Offline/PWA

`/editor` đăng ký service worker, cache app shell, autosave draft cục bộ và giữ hàng chờ save trong lúc mất mạng. Khi online trở lại, tài liệu được đồng bộ GitHub.

## V9 — Voice Dictation y khoa tiếng Việt

Web Speech `vi-VN` + chuẩn hóa lệnh dấu câu và thuật ngữ như TNF-α, APACHE II, BISAP, SOFA, CRP, OR/RR/HR/AUC.

Endpoint: `POST /api/v9/voice/normalize`.

## V10 — One-click Finalize

Chuỗi xử lý: review → sửa an toàn → xác minh DOI/PMID → track changes → định dạng → DOCX → Print-to-PDF.

Endpoint: `POST /api/v10/finalize`.

## Kiểm thử

`webapp/tests/test_advanced_v10.py` kiểm tra reviewer, track changes, template, DOCX/footnote, Evidence Table, dictation, GitHub production policy, finalize và route V3–V10. CI chạy toàn bộ regression tests khi push/PR.
