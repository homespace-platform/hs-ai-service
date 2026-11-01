# Boundary: giải thích khi không có phòng gợi ý

## Kết quả điều tra ngày 01/11/2026

Câu hỏi: “Tôi cần tìm phòng trọ Phường Gò Vấp cho 3 người dưới 4 triệu rưỡi”.

- Hội thoại lưu đúng giá dưới 4.500.000đ, sức chứa từ 3 người, loại phòng trọ và phường Gò Vấp. Chính sách là `AUTO`, không phải khách từ chối tăng ngân sách.
- Core tìm được 18 tin chính. Kiểm tra CSDL ở thời điểm điều tra: các tin công khai, còn hạn, đáp ứng sức chứa từ 3 người trong khu vực này có giá 3.300.000–4.400.000đ/tháng.
- Gọi thật `POST /api/v1/public/listings/ai/value-opportunity` với tiêu chí và 4 ID đang hiển thị: HTTP 200, trần 4.500.000đ, mức tăng tối đa 500.000đ, `candidateIds=[]`.
- Vì không có ứng viên trong khoảng boundary nên trường hợp này do dữ liệu, không phải AI bỏ mất phòng tốt. Không ép model tạo đề xuất.

Đây là kết quả tại thời điểm kiểm tra, không phải số lượng cố định của hệ thống.

## Hành vi mới

| Trạng thái | Ý nghĩa | Cách phản hồi |
| --- | --- | --- |
| `NO_CANDIDATES` | Tool hoàn tất, xác minh được ngân sách, danh sách ứng viên rỗng | “Ngoài ra, mình chưa thấy phòng nào đáng để bạn tăng thêm ngân sách.” |
| `NO_CLEAR_ADVANTAGE` | Có ứng viên nhưng model không chọn, hoặc điểm tốt chưa hơn cả nhóm đang hiển thị | “Ngoài ra, mình chưa thấy phòng nào đáng để bạn tăng thêm ngân sách.” |
| `UNAVAILABLE` | Lỗi tool, hết thời gian đánh giá, dữ kiện không xác minh được hoặc ứng viên không còn công khai | Nói chưa kiểm tra đầy đủ; không tuyên bố không có phòng phù hợp |
| `SUGGESTED` | Phương án được chọn có điểm vượt trội so với toàn bộ nhóm và được kiểm chứng | Hiển thị một đề xuất ngắn như trước, không thêm thông báo thiếu kết quả |
| `SKIPPED` | Không phải lượt tìm boundary theo quy tắc hiện tại | Không nói rằng vừa kiểm tra hoặc không có ứng viên; không thêm lời nhắc lặp vào câu trả lời tiếp nối |

Thông báo được ứng dụng dựng sau bước đối chiếu, không phụ thuộc việc model nhớ viết thêm câu giải thích.

## Quy tắc không thay đổi

- Tìm boundary ở lượt tìm đầu tiên có kết quả hoặc khi đổi phường/tỉnh. “Xem thêm”, lọc tiếp cùng khu vực và đối chiếu nhóm phòng cũ không gọi boundary lại.
- Chỉ nới trần tiền thuê tối đa 500.000đ/tháng; giữ nguyên các điều kiện cứng khác và kết quả/bảng chính.
- Nếu khách không cho vượt ngân sách hoặc không có ngân sách tháng so sánh được thì không tự tìm thêm.
- Một điểm tốt mà phòng trong nhóm chính đã có không đủ để đề xuất phòng đắt hơn. Mọi lý do hiển thị vẫn phải có bằng chứng so với cả nhóm.
- Không gọi TomTom hoặc dịch vụ bản đồ khi tư vấn boundary.

## Theo dõi và kiểm thử

`listingContext.boundaryCheck` được lưu cùng câu trả lời trong hội thoại; chỉ chứa trạng thái, lý do và số ứng viên, không lưu thêm bản sao dữ liệu phòng. Log `listing_boundary_check` mang cùng thông tin và `request_id` để phân biệt dữ liệu rỗng, model không chọn, lỗi đánh giá và lượt không cần tìm lại. Không thay đổi schema API frontend hoặc tiêu chí trong bộ nhớ tìm phòng.

`tests/unit/test_boundary_feedback.py` kiểm tra dữ liệu rỗng, model từ chối đề xuất, bằng chứng không hợp lệ, lợi thế không mới, lỗi tool/model, ngân sách thiếu khả năng đối chiếu, gợi ý thành công, phân trang, đổi khu vực và khách từ chối vượt ngân sách.

Đã chạy 931 unit tests đạt. Kiểm thử với model thật và Core API cho đúng câu hỏi phía trên: 4 tin chính, 1 lần gọi boundary, `NO_CANDIDATES`, có thông báo thân thiện; không ghi hội thoại thử vào MongoDB và không gọi TomTom.

Khởi động lại AI service để áp dụng. Không cần thay đổi CSDL hoặc khởi động lại Core API cho bản sửa này. Câu trả lời đã lưu trước đó không được viết lại.
