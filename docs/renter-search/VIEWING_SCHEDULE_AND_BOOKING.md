# Lịch tiếp khách và hướng dẫn đặt lịch trong chat

AI phân biệt ba loại thông tin, không dùng lịch khách đã đặt để trả lời lịch tiếp khách của tin đăng.

| Câu hỏi của khách | Nguồn đọc | Cách trả lời |
| --- | --- | --- |
| Tôi muốn xem lịch có thể đi xem của phòng được gợi ý. | `ListingTools.detail` → `GET /api/v1/public/listings/ai/{listingId}` | Đọc `viewingDays`, `viewingSlots`, trình bày bảng ngày trong tuần và buổi tiếp khách. |
| Các phòng trên có lịch tiếp khách thế nào? | Chi tiết từng tin trong nhóm đang xem, tối đa 5 tin/lượt | Bảng đối chiếu lịch từng phòng, không truy vấn lịch riêng của tài khoản. |
| Phòng 2 ngày mai xem được không? | `get_listing_viewing_availability` → `GET /api/v1/appointments/ai/listings/{listingId}/availability` | Đọc giờ và trạng thái thực tế do Core kiểm tra theo ngày; không tự suy ra từ lịch tiếp khách. |
| Tôi muốn đặt lịch xem phòng này, cần làm gì? | RAG tài liệu công khai đã duyệt | Hướng dẫn quy trình; không gọi Core, không yêu cầu chọn lại phòng hoặc ngày trước khi giải thích. |
| Hôm nay tôi có lịch xem nhà nào? | `search_my_appointments` | Danh sách lịch đã đăng ký thuộc tài khoản đăng nhập. |
| Phòng 2 ngày mai xem được không, và đặt lịch thế nào? | Core availability + RAG | Tách kết quả thực tế và hướng dẫn quy trình; chỉ ghi nguồn RAG khi có dẫn chứng. |

## Tham chiếu hội thoại

- “Phòng được gợi ý” dùng `listingContext.mainRecommendation.listingId`, chỉ hợp lệ trong nhóm `listingIds` hiện tại. Không chọn ngẫu nhiên hoặc lấy phòng boundary.
- Sau khi đọc lịch một phòng, lưu `viewingListingId` cùng nhóm gốc để “phòng này”, “ngày mai nhé”, “chiều mai” tiếp tục đúng phòng và đúng số thứ tự.
- Câu hỏi quy trình vẫn giữ các tham chiếu này nhưng được phân loại `KNOWLEDGE`, không thành truy vấn lịch cá nhân.
- Phòng hoặc số thứ tự không rõ: hỏi lại, không gọi API bằng ID tự suy đoán. Đổi nhóm tìm kiếm thì không dùng tham chiếu nhóm cũ.
- Chỉ lưu tham chiếu, không dùng nội dung lịch cũ trong chat làm dữ liệu hiện tại. Mỗi lần hỏi lịch lại đọc Core.

## Giới hạn

- Lịch tin đăng công bố các ngày và buổi. Chỉ sau khi khách chọn ngày, Core mới trả chính xác các khung giờ có thể gửi yêu cầu; chat không hard-code giờ hoặc cam kết giữ chỗ.
- Thiếu lịch trong tin: nói tin chưa cung cấp. Lỗi đọc tin không được trả thành “bạn chưa có lịch đăng ký”.
- Khách chưa đăng nhập vẫn đọc được lịch tiếp khách công khai và hướng dẫn RAG. Kiểm tra khả năng đặt vào ngày cụ thể và lịch riêng vẫn yêu cầu đăng nhập.
- AI chỉ đọc và hướng dẫn. AI không tự tạo hoặc xác nhận lịch thay khách/chủ nhà.
- RAG không có tài liệu phù hợp: giữ trạng thái thiếu bằng chứng, không bịa quy trình.

## Kiểm thử

`tests/unit/test_viewing_schedule_routing.py` bao gồm đúng hai câu trong báo lỗi, các biến thể không dấu, lịch nhiều phòng, thiếu ID đề xuất, số phòng ngoài nhóm, khách chưa đăng nhập và chuỗi tích hợp **đề xuất → lịch tiếp khách → hỏi quy trình → chọn ngày** qua pipeline `/agent/ask` với Core/RAG giả lập có kiểm tra đường gọi.

Các test lịch hẹn cá nhân, xác thực, quyền truy cập và khung giờ cũ vẫn được chạy trong bộ unit/eval. Không cần sửa Core API, frontend, seed hoặc cơ sở dữ liệu. Khởi động lại AI service để áp dụng.
