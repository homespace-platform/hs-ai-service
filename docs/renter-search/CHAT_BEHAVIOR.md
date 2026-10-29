# Hành vi chat tìm phòng (dùng khi test bằng giao diện)

Tài liệu này mô tả trợ lý **phải** làm gì ở từng tình huống, để bạn đối chiếu khi chạy bộ
[AI_RENTER_MANUAL_ACCEPTANCE_QUESTIONS.md](../../../hs-infra/documents/ask/AI_RENTER_MANUAL_ACCEPTANCE_QUESTIONS.md).
Kỳ vọng chi tiết cho từng ID (tiêu chí bắt buộc/cấm, câu hỏi lại, giới hạn cần nêu) nằm trong
`tests/eval/renter_corpus_v1.json`.

## Khi nào hỏi lại

- **Chưa có phạm vi tìm**: chưa có phường/tỉnh và chưa có địa điểm mốc duy nhất.
  Ví dụ "Tìm phòng có thang máy" → hỏi khu vực và **không** tìm. Trợ lý nhắc lại điều đã hiểu ("thang máy").
  Không tự lấy địa chỉ tài khoản hay tỉnh trên header. Web/Core **không** có tìm toàn quốc: phải chọn tỉnh/thành
  (hoặc địa điểm mốc) trước khi tìm; trợ lý không gợi "nói tìm toàn quốc".
- **Địa điểm mốc mơ hồ**: "gần IUH" mà có nhiều cơ sở → đưa danh sách cơ sở để chọn; khách trả lời
  ngắn "cơ sở 2" thì giữ nguyên giá/số người/tiện ích đã nói.
- **Tên quận cũ hoặc tên phường trùng** → hỏi phường mới cụ thể.
- **Mâu thuẫn trong cùng câu**, ví dụ "từ 5 đến 4 triệu" → hỏi đúng phần mâu thuẫn, giữ các điều kiện khác.
- **Đổi loại nhà làm điều kiện cũ không còn hợp** (ban công phòng trọ → căn hộ) → hỏi có bỏ điều kiện đó không.
- **Ngày mơ hồ** làm thay đổi kết quả → hỏi ngày cụ thể.
- Chỉ hỏi một hoặc hai điều quan trọng nhất. Không hỏi persona, ngân sách hay số người khi khách chưa
  đặt điều kiện đó và phạm vi đã đủ.

## Khi nào tìm

- Đủ phạm vi là tìm ngay, kể cả khi chưa nói ngân sách.
- Mọi điều kiện khách nói rõ là **bắt buộc**: "dưới 5 triệu" là < 5.000.000, "tối đa/không quá" là ≤,
  "ít nhất" là ≥, "3 người" là sức chứa ≥ 3. "Phòng trọ" là loại ROOM; "phòng/chỗ ở" chung chung không lọc loại.
- "Ưu tiên", "nếu được", "tầm/khoảng X" là **mong muốn**: được ghi nhận, giải thích, không loại tin.
- "Thoáng", "yên tĩnh", "an ninh tốt", "đi bộ 10 phút", "không ngập", "gần gym" không có dữ liệu để lọc:
  trợ lý nói rõ là chưa lọc được, không tự đổi thành điều kiện khác.
- Gym **trong tòa nhà** là tiện ích của tin; gym **gần nhà** chưa được thu thập nên phải báo chưa biết.
- Số tin hiển thị theo yêu cầu ("cho xem 10 tin"), mặc định 4, tối đa theo cấu hình (20).
- "Xem thêm / tin khác" không lặp tin đã xem, dùng **cùng bảng** với lần tìm đầu (không viết lại từng phòng
  phía trên). Đổi điều kiện thì bắt đầu lại.
- Câu hỏi về hợp đồng, thanh toán, hướng dẫn sử dụng vẫn đi nhánh hỗ trợ, không biến thành tìm phòng.

## Bảng so sánh trong câu trả lời

- Mọi kết quả tìm kiếm đều kèm **ngay** một bảng (tin là cột, tối đa 4 cột mỗi bảng; nhiều hơn thì tách
  bảng). Khách không phải hỏi thêm "lập bảng". Bảng do server dựng từ dữ liệu chi tiết đầy đủ, không do model viết.
- Tiêu đề cột ("Phòng 1") là link mở tin ở tab mới (có icon mũi tên); hàng đầu tiên là tiêu đề tin, hàng thứ hai là
  ảnh bìa (bấm ảnh cũng mở tin).
  Tin đã có link trong bảng thì web **không** hiện thẻ bên dưới nữa. Chỉ ảnh https trong bảng của server được hiển thị;
  ảnh/link do model tự viết bị bỏ.
- Số người, xe, lượng điện/nước chỉ được ghi nhận khi khách nói ra; giá trị model tự điền (1 người, 0 xe, 0 kWh)
  bị bỏ.
- Trường mặc định: Giá thuê, Diện tích, Có thể nhận, Tối đa người, Nội thất, Gửi xe máy; phòng trọ thêm WC, Bếp,
  Cửa sổ, Ban công, Gác lửng; căn hộ/nhà thêm phòng ngủ, phòng tắm, tầng/số tầng, gara. Có thêm Loại hình khi
  trộn loại, Khu vực khi khác phường, khoảng cách khi đã chọn địa điểm mốc.
- Khách nhắc nhóm trường nào thì bảng có nhóm đó, đọc đủ mọi dòng (không bị cắt như bản xem nhanh 4 KB):
  tiện ích, nội thất (món, số lượng, tình trạng), chi phí (điện, nước, quản lý, internet, rác, gửi xe…),
  điều kiện thuê (cọc, kỳ thanh toán, thuê tối thiểu), nội quy, lịch xem, vị trí. "Tất cả thông tin" lấy mọi nhóm.
  Ví dụ: "lập bảng so sánh trực quan", "thêm tiện ích và nội quy vào bảng", "so sánh chi phí các phòng trên".
- Trong lúc tìm, từ chỉ điều kiện ("có máy lạnh") là bộ lọc, không tự bung cả nhóm nội thất; khách cần hỏi
  xem ("kèm", "liệt kê", "cho xem"…).
- ‘Chưa rõ’ là tin chưa cung cấp; ‘—’ là tin không ghi mục đó hoặc mục không áp dụng cho loại hình.
- Câu nhờ chọn ("nên chọn phòng nào") vẫn có lời tư vấn của model, bảng của server nằm dưới; bảng model tự vẽ bị bỏ.

## Khi không có kết quả

Trợ lý **không tự nới** điều kiện. Câu trả lời phải: liệt kê các điều kiện đang giữ; nói số tin chưa đủ
dữ liệu để xác nhận (nếu có); xin thông tin thiếu để tính chi phí (số người, xe, điện, nước); hỏi khách muốn
đổi điều kiện nào. Chỉ bỏ đúng điều kiện khách cho phép bỏ ("bỏ thang máy" chỉ bỏ thang máy).

## Khi chỉ đọc hoặc so sánh trong các tin đã hiển thị

- "Phòng số 2 có thang máy không?", "phí xe phòng này bao nhiêu?" → đọc dữ liệu hiện tại của đúng tin đó,
  **không** thêm điều kiện vào bộ lọc.
- "So sánh phòng 1 và 3" → bảng so sánh từ dữ liệu hiện tại; không tìm tập mới, không gọi planner.
- "Trong các phòng trên, phòng nào…" → chỉ xét các tin đang hiển thị. Nếu khách đổi điều kiện (ví dụ thêm
  người), hệ thống lọc lại cả nhóm trước khi xếp hạng; tin thứ tư đủ điều kiện vẫn có thể được chọn.
- "Nếu thêm 1 người thì sao?" là câu giả định: trả lời nhưng không đổi số người cho các lượt sau.
- Tin đã ẩn/hết hạn sau khi tìm không được đưa ra lại.

## Khi nói về tiện ích xung quanh (Nearby)

- Khoảng cách là **đường chim bay**, có đơn vị, không phải thời gian đi lại.
- Khoảng cách tới một địa điểm mốc đã xác định (IUH cơ sở chính) tính từ tọa độ, kể cả ngoài 2 km.
- Dữ liệu quanh tin là mẫu đã thu thập (tối đa 100 điểm, thường là PARTIAL): có điểm phù hợp thì được nêu
  và so sánh; **không có** trong mẫu nghĩa là **chưa biết**, không phải "không có".
- Loại cụ thể phải đúng loại: nhà thuốc không phải bệnh viện, cửa hàng tiện lợi không phải siêu thị,
  mầm non không có loại riêng (gộp trong "trường học").
- Hỏi bán kính 5 km: đọc dữ liệu đã có và nói rõ giới hạn; không quét thêm, không gọi TomTom.

## Khi đề xuất

Khi khách nhờ chọn, trợ lý đưa **tối đa ba** tin, chỉ gồm tin đạt mọi điều kiện bắt buộc (còn hai thì đưa
hai). Lý do dựa trên điều khách đã nói và bằng chứng dữ liệu; nêu đánh đổi, không bịa điểm chất lượng.

## Chạy lại bộ câu hỏi bằng giao diện

1. Chạy Core API, AI service và web như bình thường với dữ liệu seed hiện tại (không cần reset DB).
2. Đăng nhập tài khoản người thuê. Mỗi câu Q001–Q120 dùng **một hội thoại mới**; Q106–Q115 gửi trước câu
   setup ghi trong corpus. Mỗi hội thoại C01–C10 gửi lần lượt 8 lượt trong cùng một hội thoại.
3. Đối chiếu với `expect` của ID đó: tiêu chí `required` phải có, `forbidden` không được có, `clarify`
   là phần cần hỏi, `limitations` là giới hạn phải nói, `maxCards` là số thẻ tối đa.
4. Chấm `PASS`/`FAIL`. Câu trả lời rỗng chỉ là `FAIL` khi seed có tin đạt mà hệ thống bỏ sót, hoặc trợ lý
   tự bỏ điều kiện/bịa lý do; nếu seed không có dữ liệu phù hợp thì ghi `MISSING_FIXTURE`.
5. Ghi kết quả theo ID (kèm `requestId` trong log nếu cần tra) vào một file riêng, ví dụ
   `tests/eval/results/manual_chat_results.json`; không sửa trạng thái `NOT_RUN` trong corpus cho tới khi
   có kết quả thật.
