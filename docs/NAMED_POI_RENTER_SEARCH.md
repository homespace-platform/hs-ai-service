# Tìm phòng quanh địa danh đã có trong kho POI

## Luồng thực hiện

1. Model chọn `search_listings` với `place.query` khi khách muốn **tìm phòng gần địa danh**. Tool `get_listing_nearby_places` chỉ xuất hiện khi đã có các listing tham chiếu; tool này dùng để đọc tiện ích quanh nhóm phòng đang xem.
2. `ListingAsk` gọi `ListingTools.resolve_place` → `GET /public/listings/ai/nearby-places/search?keyword=...`. Giữ thông tin tên, thành phố, cơ sở và địa chỉ khách cung cấp; không thay cả chuỗi bằng một alias tên trường.
3. Core chuẩn hóa dấu/cách viết tắt chung, tìm các từ trong tên POI đã lưu và dùng phần địa chỉ để phân biệt cơ sở. Các cổng của cùng địa danh được đối chiếu bằng bộ lọc trùng POI; hai cơ sở thật vẫn yêu cầu khách chọn nếu chưa đủ thông tin.
4. ID địa điểm luôn do Core trả về. Không cho model tự tạo ID hoặc tọa độ. Không tìm thấy thì hỏi thêm, không tự chuyển thành “gần một trường bất kỳ”.
5. Địa danh đã chọn đáp ứng điều kiện khu vực. Nếu còn thiếu ngân sách/số người/loại nhà, hỏi phần thiếu và lưu target cùng các điều kiện đã biết vào `searchState`.
6. Khi đủ dữ liệu, dùng `search_v2` với `nearbyAll=[{placeId,radiusMeters}]`. Core lấy ngược các listing có liên kết hợp lệ với **đúng ID POI**, rồi áp dụng giá, sức chứa và các tiêu chí khác trước `COUNT`, thứ tự và `LIMIT`. Output sử dụng cùng bảng so sánh của luồng tìm phòng thông thường.
7. “Xem thêm”, thay giá/số người và các câu hỏi so sánh tiếp tục giữ địa danh/tiêu chí từ memory. Chỉ lấy trang dữ liệu tiếp theo; không gửi toàn bộ listing về cho model lọc.

## Quan hệ POI và listing

`nearby_places` là kho địa điểm dùng chung; `listing_nearby_places` là quan hệ nhiều–nhiều phục vụ tiện ích xung quanh từng tin đăng. Tìm theo một **địa danh cụ thể** bắt buộc có dòng liên kết với đúng POI trong bảng này. Kiểm tra vị trí hiện tại của listing, phiên bản tọa độ POI, nguồn dữ liệu, mapping và phạm vi như API chi tiết listing. Không có liên kết hoặc liên kết đã cũ thì không được trả về, dù hai tọa độ rất gần nhau.

Snapshot tối đa 100 POI là phạm vi dữ liệu được phép tìm. Không mở rộng thành quét tất cả listing theo vòng tròn tọa độ. Khoảng cách chỉ dùng để lọc thêm/xếp thứ tự **bên trong tập đã liên kết**. “Xem thêm” và phòng boundary cũng phải giữ điều kiện này.

Các API chat chỉ đọc dữ liệu Core đã lưu. Không gọi TomTom, Vietmap hoặc dịch vụ bản đồ để giải quyết câu hỏi này; không quét bổ sung theo thời gian.

## Lịch sử hội thoại

Phản hồi hỏi chọn cơ sở (`pending.type=TARGET`) được lưu nguyên văn để khách còn thấy các lựa chọn sau khi mở lại chat. Không thay câu hỏi làm rõ bằng thông báo “Đã đối chiếu tiện ích...”. Quy tắc không sao chép bảng dữ liệu POI có thể hết hạn vào lịch sử/JWT vẫn được giữ cho phản hồi đọc tiện ích.

## Kiểm thử thực tế

Sau khi khởi động lại Core API và AI service, mở chat mới:

> Tìm phòng trọ gần Đại học công nghiệp HCM cho 2 người, tiền thuê dưới 4 triệu/tháng. Cho mình xem 4 phòng.

Kỳ vọng: nhận diện POI từ kho thật, bảng tối đa 4 phòng đủ điều kiện; không lấy tên trường làm keyword tiêu đề. Nếu có nhiều cơ sở thật thì hỏi rõ trước.

Tiếp tục:

> Xem thêm.

Kỳ vọng: các phòng tiếp theo, giữ target/giá/số người, không lặp trang trước.

Trường hợp tên kèm địa chỉ:

> Tìm phòng trọ gần Trường Đại Học Công Nghiệp Thành Phố Hồ Chí Minh — 12 Đường Nguyễn Văn Bảo, Hạnh Thông, Hồ Chí Minh, cho 3 người, dưới 5 triệu/tháng.

Trường hợp thiếu thông tin:

> Tôi cần tìm phòng trọ ở gần Đại học Công nghiệp Hồ Chí Minh.

Kỳ vọng: giữ địa danh và loại phòng, hỏi ngân sách/số người còn thiếu; câu trả lời ngắn như “Cho 2 người, dưới 4 triệu” tiếp tục cùng yêu cầu, không hỏi lại địa điểm.

Kiểm thử tự động: `tests/unit/test_named_place_search_workflow.py`; kiểm thử model thật opt-in tại `tests/eval/run_named_place_planner_smoke.py`. Core có `NearbyTargetMatcherTest`, `NearbyPostgresTest` và `NearbyTargetReadOnlySmokeTest`; bài smoke cuối chỉ đọc kho thật trong transaction read-only, không khởi động worker hoặc gọi nhà cung cấp.

### Đối chiếu lỗi lấy thừa phòng — 02/11/2026

Trong snapshot CSDL hiện tại, `Chi Nhánh Đại Học Ngân Hàng` có 12 liên kết listing công khai còn hợp lệ. Với câu hỏi **“Tìm phòng trọ có Chi Nhánh Đại Học Ngân Hàng trong tiện ích xung quanh, cho 3 người, dưới 4,5 triệu/tháng”**, truy vấn mới trả đúng 3 phòng. Bài smoke dùng code Core mới biên dịch và kiểm tra POI xuất hiện trong các trang tiện ích chi tiết của từng phòng trả về; không lấy vòng tròn tọa độ làm tập kết quả.

Số lượng này phụ thuộc snapshot và các điều kiện thực tế. Một POI có liên kết nhưng không có phòng thỏa giá/sức chứa vẫn phải trả 0; không mở rộng sang phòng chưa liên kết để tạo kết quả. Các test PostgreSQL còn kiểm tra xem thêm, đổi địa chỉ làm liên kết cũ hết hiệu lực, POI trùng tên khác ID, tăng bán kính và phòng boundary chưa có liên kết.

Sau khi cập nhật code cần khởi động lại **Core API và AI service**, rồi hỏi lại trong chat mới. Bảng kết quả cũ đã lưu trong lịch sử không được tự tính lại.
