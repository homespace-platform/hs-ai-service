# Tìm phòng theo tên đường

## Lỗi và cách xử lý

Planner có thể đưa “Phan Văn Trị” vào `location.ward` hoặc chỉ trả câu hỏi làm rõ, dẫn đến lỗi ghép phường/xã và mất điều kiện đường ở lượt sau. Core đã hỗ trợ field `street` trong catalog v2; không cần tạo endpoint tìm kiếm mới.

- Cụm địa chỉ rõ ràng như “đường Phan Văn Trị” được chuẩn hóa thành điều kiện cứng `street contains Phan Văn Trị`.
- Phường/tỉnh chỉ được lấy từ phần hành chính riêng của câu hỏi. “Đường Hạnh Thông” không tự biến thành “Phường Hạnh Thông”.
- Khi planner quên đường, đặt nhầm đường vào phường, hoặc hỏi làm rõ, ứng dụng vẫn giữ các điều kiện khách đã nói: đường, loại phòng, ngân sách, số người.
- Nếu chưa có tỉnh/thành hoặc khu vực đã xác nhận trong hội thoại, hỏi “Bạn muốn tìm trên đường Phan Văn Trị ở tỉnh/thành phố nào?”. Không đoán thành phố từ header hoặc địa chỉ tài khoản.
- Sau câu trả lời “Ở TP.HCM nhé”, Core nhận mã thành phố đã xác thực và điều kiện đường; không ép thêm phường vì một đường có thể đi qua nhiều phường.
- Đổi giá/số người vẫn giữ đường. Đổi sang đường khác thay điều kiện đường cũ, không AND hai đường vào cùng một tin.
- “Ở đường …” khác “gần đường …”: không biến yêu cầu gần một đường thành yêu cầu địa chỉ phải nằm trên đường đó.
- Tên đường không được ép xuất hiện trong tiêu đề; điều kiện dùng địa chỉ của tin, không chỉ dùng keyword rộng trên tiêu đề/mô tả.

## Kiểm thử

- `tests/unit/test_street_search.py`: câu gốc trong báo lỗi, planner nhầm phường, planner hỏi làm rõ, giữ điều kiện qua nhiều lượt, đổi đường, chữ không dấu ở tầng trích xuất, tên đường trùng tên phường, địa chỉ có số, phân biệt gần đường/vị trí trên đường.
- `scripts/smoke_street_search.py`: kiểm thử tùy chọn bằng model thật đang cấu hình và API public Core. Chỉ đọc catalog, search và chi tiết tin; không tạo lịch, không lưu chat, không gọi TomTom.

Chạy từ thư mục `hs-ai-service`:

```powershell
uv run --no-sync python scripts/smoke_street_search.py
```

Kết quả lưu trong `tests/eval/results/street_search_smoke.json` (không chứa key). Lượt kiểm tra ngày 01/11/2026 với dữ liệu hiện có:

| Hội thoại | Kết quả thực tế |
| --- | --- |
| Tìm phòng trọ đường Phan Văn Trị cho 3 người giá dưới 4 triệu | Giữ tiêu chí, chỉ hỏi tỉnh/thành; chưa gọi search Core. |
| Ở TP.HCM nhé | 2 tin. |
| Tăng giá lên dưới 5 triệu, vẫn cho 3 người | 7 tin. |
| Cho 2 người thôi, giá dưới 4 triệu | 17 tin. |

Số lượng phụ thuộc dữ liệu đang công khai. Script kiểm tra từng tin trả về có đúng đường, loại phòng, sức chứa và giá dưới mức đã chốt. Chữ không dấu được kiểm tra ở tầng trích xuất/định tuyến; smoke trên dữ liệu thật dùng tên đường có dấu như câu báo lỗi.

Frontend cũng sửa ghi chú trạng thái: câu hỏi làm rõ về tìm phòng không còn hiện “Chọn hợp đồng…”. Nguồn `CORE_CONTRACT` vẫn giữ ghi chú riêng cho hợp đồng.
