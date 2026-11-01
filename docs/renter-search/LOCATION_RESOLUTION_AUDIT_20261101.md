# Rà soát tìm kiếm địa điểm và nối tiếp hội thoại — 01/11/2026

## Bằng chứng và nguyên nhân

Đã đọc cuộc hội thoại lỗi đang lưu và tái hiện bằng model cấu hình thực tế. Model gửi cả `Phường An Nhơn, Thành phố Hồ Chí Minh` trong `location.ward`, đồng thời bỏ trống `location.city`. Bộ kiểm tra cũ so khớp nguyên chuỗi nên từ chối địa chỉ hợp lệ.

Ngoài ra, trạng thái chờ xác nhận chỉ lưu câu hỏi dạng văn bản. Tên phường chưa phân giải không được giữ thành dữ liệu riêng, khiến câu trả lời ngắn như “HCM nhé” phụ thuộc vào việc model đoán lại từ lịch sử.

| Vấn đề phát hiện | Xử lý |
| --- | --- |
| Model gộp phường và tỉnh/thành trong một trường, hoặc điền sai tỉnh | Địa danh khách nêu rõ ở lượt hiện tại được đối chiếu trực tiếp với danh mục và có ưu tiên cao hơn slot model đề xuất. Không đoán mã hành chính. |
| Phường trùng tên ở nhiều tỉnh | Hỏi lại tỉnh; giữ nguyên ngân sách, loại hình, số người và tên phường trong `pending.locationQuery`. Không tự lấy thành phố từ header hoặc hồ sơ tài khoản. |
| Khách chỉ bổ sung thành phố | Ghép với phường đang chờ xác nhận rồi gọi tool; áp dụng cả khi model trả `ask_clarification` thay vì `search_listings`. |
| Hội thoại cũ chưa có `locationQuery` | Khôi phục từ phát biểu của khách trong lịch sử đầy đủ; không đọc danh sách lựa chọn do assistant viết thành yêu cầu của khách. Không sửa transcript hay chạy migration. |
| Tên xã có chữ “Đường”, ví dụ Xã Đường Hoa | Không nhận nhầm từ nằm trong tên đơn vị hành chính thành tiền tố đường. |
| Tên Xã Vân Canh | Không cắt “Canh” như cụm “cạnh …” của một địa điểm gần nhà. |
| Thanh An / Thạnh An và tên khác dấu | Giữ thông tin dấu khi cần phân biệt; lọc theo tỉnh trước khi kết luận mơ hồ. |
| Tên đường/trường chứa địa danh | Tách điều kiện đường, địa chỉ hành chính và mục tiêu POI. Tên thành phố trong tên trường không tự trở thành bộ lọc tỉnh. |
| Đổi địa điểm / reset | Lượt mới rõ ràng ghi đè địa điểm cũ; reset không mang theo tên phường đang chờ. Các điều kiện thuê khác tiếp tục được giữ nếu khách không bỏ. |

## Kiểm thử thực sự đã chạy

- Toàn bộ **3.321 cặp phường/xã–tỉnh/thành** trong snapshot Vietmap đóng gói đều phân giải về đúng mã.
- Toàn bộ `tests/unit tests/eval`: **973 bài đạt**. Gồm hồi quy các tool, bộ lọc giá/sức chứa, điều hướng trang, nearby, hội thoại, RAG, hợp đồng và lịch hẹn trong bộ kiểm thử hiện có. Đây không phải chứng nhận rằng mọi cách diễn đạt tự nhiên đều đã được kiểm thử live.
- Smoke **6 lượt dùng model thật và Gateway/Core thật**, không dùng fake cho planner hoặc kết quả tìm kiếm:
  - Phường An Nhơn chưa có tỉnh → hỏi lại, không tìm rộng.
  - Bổ sung đầy đủ phường An Nhơn, Thành phố Hồ Chí Minh → gọi Core đúng mã phường/tỉnh, hiển thị phòng.
  - Bổ sung ngắn “TP.HCM nhé” → giữ phường, dưới 5 triệu và 2 người, hiển thị phòng.
  - Nêu đầy đủ địa chỉ ngay lượt đầu → tìm trực tiếp.
  - Đổi sang An Nhơn ở Gia Lai → đổi đúng tỉnh, giữ điều kiện; báo không có kết quả khi Core không có phòng phù hợp.
- Báo cáo live: `tests/eval/results/location_search_smoke_20261101.json`.
- Smoke chỉ đọc public listing API; không ghi cuộc hội thoại vào Mongo, không gọi TomTom. Có sử dụng model tính phí theo cấu hình backend.

Chạy lại smoke khi cần (PowerShell):

```powershell
$env:HS_AI_LIVE_EVAL='1'
uv run --no-sync python -m tests.eval.run_location_search_smoke --confirm-paid
```

## Nearby trống sau khi đổi địa chỉ

Đã kiểm tra tin `30e2776a-9b07-1e1c-08e3-e1bfacffc02c`: tác vụ vị trí mới hoàn tất lúc **22:04:24 ngày 01/11/2026**, một phản hồi 100 điểm, còn **99 điểm** sau lọc trùng. Không phải backend không fetch.

Frontend trước đây đọc một lần khi mở trang, nên có thể đọc trước khi tác vụ nền kết thúc và giữ màn hình trống. Bản sửa:

- Core trả thêm `syncStatus` của job ứng với **đúng hash vị trí hiện tại**; job thành công ở địa chỉ cũ không được dùng để báo hoàn tất.
- Frontend hiển thị đang cập nhật, đọc lại endpoint CSDL mỗi 3 giây, tối đa 20 lần tiếp theo. Hoàn tất/lỗi terminal thì dừng; đổi yêu cầu hoặc rời trang thì hủy.
- Hết thời gian theo dõi sẽ hiển thị nút kiểm tra lại, không hứa tự cập nhật vô hạn.
- Các request đọc không tạo job, không fetch TomTom, không bật lịch quét 24 giờ.
- Kiểm thử PostgreSQL riêng xác nhận chuyển trạng thái theo thay đổi tọa độ, không tự enqueue khi đọc và không dùng job vị trí cũ.

## Áp dụng

Khởi động lại AI Service và Core API để nạp mã mới; frontend dev nạp lại mã. Không cần drop CSDL hoặc sửa dữ liệu khách đã lưu. Sau khi cập nhật, có thể tiếp tục hội thoại cũ bằng câu trả lời địa điểm rõ ràng hoặc ngắn nếu tên phường đã được giữ/khôi phục.
