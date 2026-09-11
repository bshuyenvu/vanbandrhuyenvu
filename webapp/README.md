# VBHC AI Incoming/Reply V1

Module web bổ sung cho repository `vanbandrhuyenvu`:

1. Nhận PDF/DOCX/TXT/ảnh hoặc văn bản dán trực tiếp.
2. AI phân tích cơ quan gửi, số/ngày, hạn xử lý và từng yêu cầu cần trả lời.
3. Rule local trích `protected_facts` (số VB, ngày, tiền, %, căn cứ, deadline).
4. AI soạn dự thảo theo từng yêu cầu; dữ liệu thiếu phải để `[CẦN BỔ SUNG: ...]`.
5. AI review độ bao phủ, protected facts, nhất quán và căn cứ.
6. Xuất DOCX bằng `scripts/vbhc_doc_builder.py`, không tự dựng thể thức Word.

## AI router

Mặc định `auto`: Gemini trước, OpenAI fallback.

```bash
export GEMINI_API_KEY='...'
export OPENAI_API_KEY='...'              # optional
export VBHC_AI_PROVIDER='auto'            # auto | gemini | openai
export VBHC_GEMINI_MODEL='gemini-3.5-flash-lite'
export VBHC_OPENAI_MODEL='gpt-5.6-luna'
```

## Chạy thử

```bash
python -m venv venv
venv/bin/pip install starlette uvicorn python-docx pypdf
venv/bin/python webapp/server.py --host 127.0.0.1 --port 8767
curl http://127.0.0.1:8767/healthz
```

Mở reverse proxy domain vào `127.0.0.1:8767` để dùng giao diện.

## Giới hạn an toàn V1

- Xuất DOCX tự động hiện chỉ bật cho khối Nhà nước qua engine Nghị định 30 đang có.
- Chế độ Đảng dùng được cho phân tích/soạn/review, nhưng backend chặn xuất DOCX cho đến khi rule pack Quy định 399-QĐ/TW + Hướng dẫn 05-HD/VPTW được kiểm định.
- Khi dùng Gemini/OpenAI, nội dung gửi tới API của nhà cung cấp AI. Với tài liệu mật/nội bộ, cần chính sách dữ liệu và lớp xác thực/reverse proxy phù hợp trước khi mở ra Internet.
