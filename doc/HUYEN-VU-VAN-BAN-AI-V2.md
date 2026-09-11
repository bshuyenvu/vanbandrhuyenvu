# Huyền Vũ Văn Bản AI V2

## Mục tiêu

Biến lõi soạn thảo VBHC hiện có thành nền tảng quản lý văn bản và AI dùng cho cá nhân, phòng/khoa và cơ quan/đơn vị. V2 giữ các renderer/rule deterministic; AI chỉ phân tích, dự thảo, kiểm định và gợi ý.

## Các lớp chính

- **Identity**: đăng ký, kích hoạt, đăng nhập, session ký HMAC.
- **Multi-tenant**: User → Organization → Department/Khoa → Membership.
- **RBAC**: Platform Admin, Owner, Admin cơ quan, Văn thư, Lãnh đạo, Trưởng phòng/khoa, Thành viên, Billing, Auditor, Viewer.
- **DMS**: văn bản đến, đi, nội bộ; liên kết văn bản nguồn/trả lời; phiên bản; giao việc; deadline; trạng thái.
- **Workflow**: received/draft → assigned/processing → submitted → approved/rejected → issued/closed → archived.
- **AI Control**: Model Registry, tier Economy/Standard/Advanced/Private, routing theo task + gói + policy + chi phí + daily budget.
- **Billing**: Subscription, Wallet, Credit Ledger, AI Usage, payment order; 1 AI Credit được tính như một đơn vị chi phí nội bộ gần 1 VND và được tính động từ giá provider.
- **Audit**: ghi thay đổi tài khoản, cơ quan, thành viên, workflow, billing và model.

## Giao diện

- `/` — Workspace/đăng nhập, dashboard, văn bản đến/đi, phòng-khoa, thành viên, gói và model.
- `/reply` — AI phân tích văn bản đến, soạn phúc đáp, review, xuất Word.
- `/billing` — người dùng chọn gói và theo dõi payment order; Platform Admin có thể xác nhận đơn.
- `/admin-control` — Platform Admin quản lý tài khoản, gói, credit, tổ chức, AI models và usage.

## Thể loại văn bản

Registry V2 có các nhóm Nhà nước và Đảng. Xuất Word chỉ bật khi có renderer đã kiểm định:

- Nhà nước: Công văn, Báo cáo, Tờ trình, Kế hoạch, Quyết định, Thông báo.
- Đảng: Công văn/phúc đáp.
- Các loại Đảng còn lại đã đăng ký trong registry nhưng giữ `enabled_export=false` cho đến khi kiểm định template riêng theo 399-QĐ/TW + 05-HD/VPTW.

## Chính sách dữ liệu

Mỗi tổ chức có một mức: `public`, `internal`, `confidential`, `restricted`.

- `restricted`: chỉ cho model `private/local`; không gửi tới OpenAI/Google.
- `confidential`: mặc định được xử lý như `restricted` trừ khi Admin chủ động bật `VBHC_ALLOW_CONFIDENTIAL_EXTERNAL_AI=true`.
- Anonymous AI mặc định tắt.
- Protected Facts từ V1 tiếp tục khóa số văn bản, ngày, số liệu, tiền, căn cứ và deadline.

## AI Credit

Mỗi AI request ghi:

`input_tokens + cached_tokens + output_tokens → provider_cost_usd → tỷ giá → reserve → service multiplier → charged_credits`

Cấu hình chi phí nằm trong Model Manager, không hard-code vào gói. Admin có thể bật/tắt model, đổi tier, cập nhật giá token, hệ số dịch vụ và daily budget mà không deploy lại code.

## Gói khởi điểm

Các mức Free / Personal / Professional / Team / Organization trong `webapp/saas/catalog.py` là cấu hình thử nghiệm. Trước khi công bố thương mại cần theo dõi chi phí thật trong `ai_usage` và điều chỉnh credit/gross margin.

## Database

- Runtime bootstrap hiện dùng SQLite để dễ triển khai trên server nhỏ.
- Production schema PostgreSQL có tại `webapp/saas/schema.sql`.
- Bước production tiếp theo là viết repository adapter PostgreSQL và migration dữ liệu; không thay đổi API domain model.

## Cài V2

```bash
cd /home/mcp-soan-thao-vbhc
git checkout main
git pull origin main
sudo bash deploy/install-web.sh
```

Installer tạo `/etc/vbhc-web.env`, session secret ngẫu nhiên, DB directory và service `vbhc-web` tại `127.0.0.1:8767`.

Sau đó điền API key và email Platform Admin vào `/etc/vbhc-web.env`, restart service, rồi bootstrap admin:

```bash
sudo systemctl restart vbhc-web
venv/bin/python scripts/bootstrap_hv_admin.py --email YOUR_EMAIL
```

## Trước khi mở Internet

1. Reverse proxy HTTPS + rate limiting.
2. Cấu hình Platform Admin và API keys.
3. Chuyển runtime DB sang PostgreSQL.
4. Kiểm thử backup/restore và audit retention.
5. Cấu hình payment gateway/webhook nếu cần tự động xác nhận.
6. Thêm MFA/SSO cho Organization/Enterprise.
7. Kiểm thử với bộ văn bản thật của từng thể loại trước khi bật renderer.
8. Không đưa tài liệu mật/restricted ra external AI.

## Trạng thái V2 hiện tại

V2 là SaaS Foundation + DMS MVP. Nó đã có lớp dữ liệu, auth/RBAC, document workflow, billing/credit, Model Manager, Admin UI, AI usage meter và các renderer V2 nêu trên. Chữ ký số, PostgreSQL runtime, cổng thanh toán tự động, local/private LLM và RAG pháp lý toàn diện là các phase kế tiếp.

## Dung lượng hồ sơ SSD

Tệp gốc và tệp đính kèm được lưu ngoài Git tại `VBHC_FILE_ROOT`. Hệ thống tính dung lượng từ metadata tệp và chặn tải lên trước khi vượt hạn mức. Mặc định: Miễn phí 100 MB, Cá nhân 1 GB, Chuyên nghiệp 5 GB, Nhóm 20 GB; gói Cơ quan do quản trị viên nền tảng cấu hình. Dashboard hiển thị Đã dùng / Tổng / Còn lại cho không gian đang chọn.
## V2.3 — Tiếp nhận & số hóa theo lô

- Màn hình `/digitize` nhận tối đa 10 tệp mỗi lượt và xử lý OCR tuần tự.
- Trình duyệt tính SHA-256, backend đối chiếu tệp đã lưu trước khi gọi AI để tránh tốn token cho bản trùng.
- Sau OCR, hệ thống kiểm tra thêm số/ký hiệu, ngày ban hành, cơ quan gửi và độ tương đồng trích yếu; hồ sơ nghi trùng bắt buộc cán bộ xác nhận.
- AI phân loại loại văn bản, mức khẩn và đề xuất đơn vị xử lý; backend ánh xạ đề xuất vào danh mục phòng/khoa và có thể tham chiếu lịch sử xử lý.
- API `intake/commit` kiểm tra định dạng tệp, quota SSD, trùng lặp, loại văn bản và quyền phân công trước khi tạo hồ sơ + tệp gốc + phân công trong một thao tác nghiệp vụ.
- Ghi sổ nguyên khối giúp tránh trạng thái văn bản đã tạo nhưng tệp gốc tải lên thất bại.

## V2.3.1 — Công việc, hạn xử lý và trình duyệt

- Trang `/tasks` gom công việc theo Chờ giao / Đang xử lý / Chờ duyệt / Chờ phát hành / Hoàn tất.
- Hạn xử lý được tính theo múi giờ Việt Nam và cảnh báo Quá hạn / còn ≤24 giờ / còn ≤48 giờ.
- Dashboard hiển thị số việc quá hạn, sắp hạn và chờ duyệt của phạm vi người dùng được quyền xem.
- Chuyên viên chỉ xem và chuyển trạng thái hồ sơ mình được giao; Trưởng phòng/khoa theo phạm vi đơn vị; Lãnh đạo có hàng chờ duyệt; Văn thư có hàng chờ phát hành.
- API không dựa vào việc ẩn nút trên giao diện: kiểm tra quyền được thực hiện lại ở backend trước mọi thay đổi trạng thái.

## V2.3.1 — Công việc, hạn xử lý và duyệt theo vai trò

- Trang `/tasks` gom công việc theo Chờ giao, Đang xử lý, Chờ duyệt, Chờ phát hành và Hoàn tất.
- Cảnh báo hạn dùng múi giờ Việt Nam: Quá hạn, còn tối đa 24 giờ và còn tối đa 48 giờ.
- Chuyên viên chỉ thấy và thao tác hồ sơ do mình tạo hoặc được giao; trưởng phòng giới hạn theo phòng/khoa; Văn thư, Lãnh đạo và Quản trị theo đúng quyền cơ quan.
- Dashboard hiển thị nhanh số hồ sơ quá hạn, sắp hạn và chờ duyệt.
- API chi tiết, danh sách, tệp đính kèm, phiên bản và chuyển trạng thái đều kiểm tra phạm vi hồ sơ ở phía server.
