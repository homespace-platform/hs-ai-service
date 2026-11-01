# Kế hoạch nâng chất lượng hội thoại AI HomeSpace

Ngày rà soát: **01/11/2026** (giờ Việt Nam). Trạng thái: **đã triển khai sau khi được duyệt; đã kiểm thử offline và smoke model có giới hạn**.

Tài liệu này ghi nhận phân tích ban đầu và hướng triển khai về so sánh phí, kết luận theo từng ưu tiên và câu hỏi gợi ý theo ngữ cảnh cho tìm phòng, hợp đồng, lịch hẹn. Các phát hiện source bên dưới mô tả thời điểm khảo sát. Xem [báo cáo triển khai và phạm vi kiểm thử](AI_CONVERSATION_QUALITY_IMPLEMENTATION.md) để biết thay đổi thực tế và các bước UAT còn cần chạy.

Bộ tình huống nghiệm thu: [AI_CONVERSATION_QUALITY_ACCEPTANCE_CASES.md](AI_CONVERSATION_QUALITY_ACCEPTANCE_CASES.md).

**Điều chỉnh yêu cầu sau triển khai (01/11):** phần so sánh phí chỉ đối chiếu đơn giá, không tính/hiện chi phí cố định hay tổng tháng, không hỏi thêm số xe để tính tổng và không gợi hỏi Nearby sau chủ đề phí. Câu hỏi liệt kê tiện ích xung quanh chỉ trả bảng; chỉ chọn phòng khi khách yêu cầu tư vấn rõ ràng. Quy tắc này thay thế các đề xuất tự tính tổng/tự chọn phòng trong kế hoạch khảo sát bên dưới.

## 1. Kết luận từ source hiện tại

Vấn đề không chỉ nằm ở system prompt. Hệ thống đã có tool đọc dữ liệu, bảng do server dựng, memory tìm phòng và kiểm tra bằng chứng. Tuy nhiên, **nhận diện mục đích câu hỏi → chọn bằng chứng → chọn phòng → kết luận → gợi ý hỏi tiếp chưa dùng chung ngữ cảnh của lượt hiện tại**. Sửa thêm từng câu prompt sẽ tiếp tục để lọt lỗi ở nhánh khác.

### 1.1. Các phát hiện đã xác minh

Các đường dẫn trong bảng tính từ thư mục `hs-ai-service`, trừ khi ghi rõ repo khác. Số dòng là tại thời điểm khảo sát; tên hàm là mốc chính khi source thay đổi.

| Mã | Phát hiện | Bằng chứng source | Hệ quả |
| --- | --- | --- | --- |
| F01 | “Khác nhau” làm câu so sánh phí đi vào nhánh sinh văn bản, dù bảng đã đủ dữ liệu | `application/use_cases/listing_ask.py:63` (`ADVICE_REQUEST`), `:687` (`_present`): sinh `raw` rồi mới ghép bảng | Đoạn văn liệt kê cả bốn phòng xuất hiện trước bảng, lặp các ô và làm câu trả lời dài |
| F02 | Chọn phòng sau câu hỏi về phí vẫn dùng toàn bộ facts phòng | `listing_ask.py:347`, `:381` (`_advise_main`); `listing_value_advice.py:442` (`evaluate`) chỉ có focus chuyên biệt `NEARBY` | Một câu hỏi chi phí có thể nhận kết luận từ tiêu chí khác hoặc một khoản phí đơn lẻ |
| F03 | Kiểm chứng một lý do chính mới yêu cầu tốt hơn ít nhất một phòng, không kém phòng khác ở field đó | `listing_value_advice.py:387` (`useful_main_fields`): `1 in changes and -1 not in changes` | Phòng 2, 3, 4 cùng được miễn phí gửi xe đều được phép dùng lý do này. Nó phân biệt với phòng 1, nhưng **không giải thích tại sao chọn phòng 3 thay phòng 2 và 4** |
| F04 | Mô hình có thể chọn một ID nhưng không còn lý do hợp lệ sau kiểm chứng ở nhánh tổng quát | `listing_value_advice.py:565` (`_main_recommendation`): chỉ focus Nearby buộc phải có reasons; `:652` (`render_main`) có câu chọn chung khi reasons rỗng | Vẫn có lời khuyên mà khách không kiểm tra được cơ sở lựa chọn |
| F05 | Câu hỏi “tiết kiệm / rộng / tiện nghi” là câu nối tiếp cố định | `application/renter_guidance.py:187` (`followup_question`), được thêm trong `_advise_main` | Hệ thống hỏi lại điều có thể trình bày ngay thành các phương án ngắn theo dữ liệu đã có |
| F06 | Gợi ý dưới thanh chat không biết vừa trả lời chủ đề gì | `renter_guidance.py:202` (`suggested_questions`) chỉ nhận state và vài cờ; `has_boundary` chưa được dùng | Sau tìm phòng, phí, Nearby, giải thích vẫn có thể hiện cùng bộ bốn câu; gợi hỏi lại điều vừa trả lời |
| F07 | Chưa có bước hoàn thiện gợi ý chung cho các nhánh nghiệp vụ | `api/v1/agent_ask.py:95` (`generate_answer`) trả sớm ở Appointment/Contract; `contract_ask.py` và `appointment_ask.py:939` không gán `suggested_questions` | Schema đã hỗ trợ gợi ý nhưng hợp đồng/lịch hẹn không có cơ chế tương ứng với tìm phòng |
| F08 | Frontend đã có đủ cơ chế hiển thị gợi ý do backend cung cấp | `hs-web-app/lib/ai-suggested-questions.ts:9`, `components/chat/AiChatWindow.tsx:373`: lấy message mới nhất, bấm gọi `fillDraft` | Không cần xây lại thanh chat, không tự gửi câu hỏi, không cần mẫu form có chỗ trống |
| F09 | Chỉ lưu ID và vài field của lựa chọn chưa đủ mô tả một kết luận theo tình huống | `listing_ask.py` lưu `mainRecommendation` với ID/fields, focus hiện chỉ có Nearby; `conversation_listing_memory.py` cung cấp tám message gần nhất và state tìm phòng bền vững | “Vì sao chọn?” cần biết lựa chọn đang giải thích là về phí, tiện ích, không gian hay nhu cầu người thuê; không được đổi cơ sở sau đó |
| F10 | Core có tính phí nhưng AI chưa có projection so sánh phí độc lập, đầy đủ cho một shortlist | `hs-core-api/.../AiListingSearchService.java`: `Computed`, `evidence`; `AiListingSearchResponse.ListingEvidence` chỉ trả actual cho điều kiện đã biên dịch. `ListingTools` chưa có tool ước tính phí | Không thể giải quyết tính tổng chính xác chỉ bằng việc sửa lời văn của model |
| F11 | API ước tính yêu cầu thuê hiện tại không phải tool thay thế trực tiếp cho so sánh phí | Core `RentalEstimateRequest`: bắt buộc `moveInDate`, `leaseMonths`; AI chưa biết các giá trị này khi chỉ hỏi phí | Không tự điền ngày/thời hạn giả để gọi `/rental-requests/estimate` |
| F12 | Một phần docs cũ đã lệch hành vi hiện tại | `docs/renter-search/CHAT_BEHAVIOR.md`: “đủ phạm vi là tìm ngay”, “không hỏi ngân sách/số người”, “tối đa ba tin”; code intake và bảng hiện tại đã khác | Cần cập nhật tài liệu gốc khi triển khai, tránh AI Agent đọc các quy tắc mâu thuẫn |

### 1.2. Kiểm tra tái hiện đã chạy

Không dùng API trả phí, không đọc/ghi dữ liệu khách, không gọi TomTom:

- Gọi trực tiếp `suggested_questions`: các câu sau kết quả có main recommendation giống hệt nhau dù có/không có boundary. Hàm không nhận chủ đề vừa trả lời nên không thể phân biệt phí với Nearby.
- Fixture bốn phòng có `includedInRent` gửi xe lần lượt `false, true, true, true`: `useful_main_fields` cho phép cả phòng 2, 3, 4 dùng phí xe như lý do tốt. Đây là lỗ hổng phân biệt lựa chọn, không phải model đọc sai giá trị field.
- Chạy lại bảy nhóm test hiện hữu về guidance, privacy/language, Nearby focus, bảng, giải thích, hợp đồng, lịch hẹn: **272 passed**. Chúng bảo vệ nhiều hành vi cũ, nhưng chưa nghiệm thu các yêu cầu mới trong tài liệu này.

Lệnh đã chạy:

```powershell
cd D:\Workspace\homespace\hs-ai-service
uv run --no-sync pytest tests/unit/test_renter_guidance.py tests/unit/test_recommendation_privacy_and_language.py tests/unit/test_nearby_recommendation_focus.py tests/unit/test_multi_result_tables.py tests/unit/test_main_recommendation_explanation.py tests/unit/test_contract_ask.py tests/unit/test_appointment_ask.py -q --tb=short
```

## 2. Hành vi sản phẩm cần đạt

### 2.1. Cấu trúc trả lời

Với nhiều kết quả: **một câu dẫn → một bảng đúng chủ đề → kết luận ngắn → gợi ý câu hỏi tiếp theo trên thanh chat**.

- Không kể lại từng phòng bằng văn xuôi trước bảng. Không vừa có đoạn mô tả bốn phòng vừa có bảng cùng dữ liệu.
- Bảng tìm mới vẫn có thông tin nhận diện/ảnh như hiện tại. Bảng hỏi tiếp về phí, Nearby, lý do chọn chỉ giữ các cột/hàng phục vụ câu hỏi; không lặp lại ảnh và toàn bộ thông tin cơ bản khi không cần.
- Bảng giữ đúng ID/số thứ tự đang xét; khi có nhiều hơn bốn phòng thì chia bảng theo component hiện có.
- Kết luận thông thường tối đa hai câu. Nếu khách muốn cân nhắc nhiều mục tiêu, dùng **bảng kết luận nhỏ tối đa ba dòng**, mỗi dòng một ưu tiên. Đây là phương án có điều kiện, không phải ba lời khuyên tổng quát mâu thuẫn.
- Câu đơn lẻ, lỗi, cần làm rõ hoặc hướng dẫn các bước vẫn có thể dùng lời ngắn/gạch đầu dòng. Không ép mọi phản hồi thành bảng.
- Không nhắc lại lời hướng dẫn “bấm tên hoặc ảnh...” ở mọi lượt khi link đã rõ.

### 2.2. Quy tắc chọn phòng và lý do

Tách rõ **sự thật của phòng**, **lợi ích chung**, **khác biệt giúp lựa chọn**:

| Tình huống | Được nói | Không được dùng để kết luận |
| --- | --- | --- |
| Cả bốn cùng có một tiện ích | Đưa trong bảng nếu đang hỏi nhóm đó | “Chọn phòng 3 vì có tiện ích này” |
| Phòng 2, 3, 4 cùng miễn phí xe; phòng 1 thu phí | Nêu phòng 2–4 cùng tiết kiệm khoản này so với phòng 1 | Chỉ vì miễn phí xe mà chọn riêng phòng 3 thay 2, 4 |
| Phòng 3 có tiền thuê thấp hơn cả nhóm; miễn phí xe như 2, 4 | Có thể chọn theo **tiền thuê thấp hơn**, nếu đúng trọng tâm và không vi phạm nhu cầu | Biến giá thuê thấp nhất thành “tổng tiền thấp nhất” khi chưa đủ dữ liệu |
| Phòng 4 rộng nhất nhưng giá cao hơn phòng 3 | Chọn khi xét ưu tiên không gian, nêu ngắn chênh lệch cần cân nhắc | Khẳng định tốt nhất mọi mặt |
| Một field của đối thủ chưa khai báo | Ghi chưa có thông tin, dùng bằng chứng khác | Coi đối thủ không có hoặc mặc định bằng 0 |
| WC/bếp/ban công/lối đi/đồng hồ dùng chung | Mô tả đúng và nêu đánh đổi nếu liên quan | Coi việc dùng chung là lợi thế riêng tư |
| Các lựa chọn ngang nhau theo mục tiêu hiện tại | Nêu đồng hạng hoặc chưa có khác biệt để chọn riêng | Ép một ID và bịa lý do riêng |

Không sửa bằng một bảng điểm cứng chỉ ưu tiên WC/diện tích. Model được đánh giá các dữ kiện **liên quan đến câu hỏi và nhu cầu đã nói**, nhưng ứng dụng phải kiểm tra chứng cứ của kết luận cuối.

Đối với lời chọn chính, không bắt buộc mỗi field đều vượt toàn bộ nhóm: một tổ hợp có thể hợp lý. Tuy nhiên, phải giải thích được vì sao tổ hợp đó giúp chọn so với **những phòng cạnh tranh gần nhất**, không dùng một ưu điểm chung của chính nhóm cạnh tranh làm lý do duy nhất. Nếu chỉ có một nhóm đồng hạng, nêu nhóm hoặc cân nhắc thêm dữ liệu; không tự đổi mục tiêu để phá hòa.

Boundary vẫn khắt khe hơn: lợi ích hiển thị phải là nâng cấp có căn cứ so với **toàn bộ trang kết quả chính**, chênh tối đa 500.000đ từ trần ngân sách; không vì thay đổi này mà nới quy tắc boundary.

### 2.3. Trả lời câu so sánh phí trong ảnh

Câu hỏi: **“Trong các phòng này, các khoản phí hằng tháng khác nhau thế nào?”**

1. Đọc lại đúng shortlist, lấy charges đầy đủ và context đã xác nhận.
2. Hiện bảng phí trước: tiền thuê, điện, nước, Internet, dịch vụ/rác, xe và các phí thật sự có. Khoản giống nhau có thể gộp vào một ghi chú ngắn; không dùng làm lợi thế.
3. Không tự giả định số xe, điện, nước; không nhân tiền thuê theo sức chứa tối đa. Ba người đã nói trong hội thoại được giữ lại.
4. Kết luận trả lời thẳng khác biệt chi phí; phần lựa chọn theo ba ưu tiên dùng dữ liệu phòng đã đọc, không hỏi lại câu ba lựa chọn cố định.

Ví dụ minh họa theo dữ liệu trong ảnh, **không phải khẳng định dữ liệu Core hiện tại**:

> Phòng 3 có tiền thuê thấp nhất; phòng 1 có đơn giá nước thấp hơn và đã gồm Internet. Phòng 2, 3, 4 đều không thu phí xe riêng, nên khoản này không tạo lợi thế riêng cho phòng 3.

| Nếu bạn ưu tiên | Lựa chọn có thể cân nhắc | Lý do ngắn theo ảnh |
| --- | --- | --- |
| Tiết kiệm tiền thuê | Phòng 3 | 3,3 triệu/tháng, thấp nhất nhóm; chưa khẳng định tổng tiền sau điện nước thấp nhất |
| Không gian rộng | Phòng 4 | 37 m², rộng nhất nhóm; tiền thuê cao hơn phòng 3 là 500.000đ/tháng |
| Thuận tiện nấu ăn | Phòng 1 | Có bếp riêng và đã gồm Internet; cần cân nhắc nhà vệ sinh dùng chung |

Không gọi đây là “phòng tiện nghi nhất” nếu chỉ xác minh được bếp/Internet. Nếu cần biết số xe để tính tổng, hỏi đúng **số xe**, không lặp câu “tiết kiệm hay rộng hay tiện nghi”. Nếu khách chỉ hỏi đơn giá, không chặn bảng để bắt cung cấp lượng điện nước.

### 2.4. Chi phí phải đúng nghiệp vụ

Phân biệt ít nhất:

- **Tiền thuê**: chuẩn hóa theo phòng/người/tháng bằng số người đã khai báo.
- **Phần cố định đã tính được**: gồm tiền thuê và phí có đầy đủ cơ sở tính; nêu phần còn thiếu, không đặt tên là tổng đầy đủ.
- **Tổng cố định**: chỉ xác nhận khi đủ toàn bộ context cần thiết và phí có thể tính.
- **Tổng ước tính**: chỉ gồm điện/nước khi khách cung cấp mức dùng hoặc chủ động chọn một kịch bản rõ ràng. Các kịch bản do trợ lý nêu không tự thành nhu cầu thật.
- **Cọc/tiền đầu kỳ**: trình bày riêng, không cộng thành chi phí hằng tháng.

Kiểm tra `includedInRent`, `billingMethod`, `amount`, đơn vị, `active`, thời điểm áp dụng và khả năng nhận xe. `0`, miễn phí, đã gồm, không áp dụng, tự đăng ký và chưa có thông tin phải giữ nghĩa khác nhau. Không so sánh hai custom fee chỉ vì cùng mang loại `OTHER`.

Tái sử dụng logic Core hiện có. Trước khi thêm API/tool tính phí, đối chiếu `AiListingSearchService.Computed` với `RentalCostCalculator` và bộ test nghiệp vụ. Không sao chép công thức tiền tệ sang nhiều prompt/Python renderer. Nếu cần, mở rộng **projection đọc theo scope** trả breakdown, phần đã tính, phần thiếu và actual computed bằng `BigDecimal`; không nhét điều kiện lọc giả vào search để lấy số tính toán, không dùng ngày/thời hạn thuê giả cho API estimate. Chốt một cách mở rộng nhỏ nhất trong bước P2, không tạo một module thanh toán mới.

## 3. Thiết kế thay đổi có giới hạn

### 3.1. Một ngữ cảnh trả lời cho mỗi lượt

Thêm kiểu dữ liệu nội bộ nhỏ (tên gợi ý `AnswerContext`), dùng lại các domain context có sẵn, không thay toàn bộ router:

| Thuộc tính | Mục đích |
| --- | --- |
| `domain` | LISTING / CONTRACT / APPOINTMENT / KNOWLEDGE / RENTAL_REVIEW |
| `intent` | SEARCH, COMPARE_COST, COMPARE_SPACE, COMPARE_AMENITIES, NEARBY, PROFILE_ADVICE, EXPLAIN_CHOICE, DETAIL, LIST, AVAILABILITY, ACTION_GUIDANCE... |
| `scope` và số thứ tự | Các ID thật đang được hỏi; listing/contract/appointment không dùng chung một hệ ordinal |
| `answeredTopics` | Phần nào đã trả lời ở lượt này để không gợi hỏi lại cùng nội dung |
| `missingInputs` | Thông tin thật sự cần cho câu trả lời tiếp theo, không tự tạo nhu cầu |
| `recommendation` | Chọn gì, theo mục tiêu nào, so với ai, field/bằng chứng nào, đâu là đồng hạng |
| `resultState` | Có kết quả / rỗng / hết trang / cần làm rõ / lỗi / cần đăng nhập |
| `allowedNextActions` | Khả năng đã được Core/tool xác minh, không do model đoán |

Ý định phải xét câu hiện tại cùng context, hỗ trợ diễn đạt tự nhiên; regex chỉ là shortcut bảo thủ. Không thêm hàng loạt regex riêng cho mỗi câu ví dụ. Câu mơ hồ cần hỏi đúng chỗ thay vì gán một intent chắc chắn.

### 3.2. Bằng chứng đúng chủ đề và kết luận có kiểm chứng

- Mở rộng cách focus Nearby đã có thành cơ chế dùng chung, tối thiểu COST, SPACE, AMENITIES, PROFILE, OVERALL, NEARBY.
- COST: chuẩn hóa phí, đơn vị và context; không dùng WC/ban công làm lý do thắng về chi phí. Default privacy không được vô tình loại phương án khi khách chỉ hỏi phương án rẻ hơn.
- SPACE: diện tích/bố trí/sức chứa phù hợp; không suy “rộng, thoáng” chỉ từ ảnh hoặc số tầng.
- AMENITIES: tiện ích trong tòa nhà và nội thất tách biệt Nearby; thiếu bản ghi không mặc định là không có.
- PROFILE: đánh giá hoàn cảnh thật đã nói, không áp khuôn sinh viên/gia đình/nghề nghiệp; giữ cả yếu tố trong phòng và POI đúng nhu cầu.
- NEARBY: giữ bảo vệ đã có; chỉ dùng địa điểm/bằng chứng liên quan. Không dùng bếp, Internet để kết luận về vị trí.
- Lời chọn chính phải có cơ sở khác biệt hữu ích. Tổ hợp lý do được đối chiếu với tất cả tin trong scope, ghi nhận đồng hạng/đánh đổi. Bỏ lời khuyên khi không chứng minh được, thay vì tự chọn bằng ID/thứ tự.
- Model trả lựa chọn, mục tiêu, proof field/giá trị/tham chiếu hợp lệ bằng JSON hạn chế. Ứng dụng xác minh phép tính, đúng scope, tính liên quan và câu chữ tuyệt đối như “nhất”. Renderer lấy giá trị từ bằng chứng, không lấy số model tự sinh.
- Sử dụng lại một lần đánh giá có cấu trúc để tạo các phương án ưu tiên, không gọi ba model/tool riêng cho tiết kiệm, không gian và tiện nghi. Chỉ lập các dòng có dữ liệu; không đủ bằng chứng thì ghi giới hạn ngắn hoặc bỏ dòng.
- Các phương án điều kiện không tự đổi `needs`, `valuePriorities`, persona hoặc ngân sách đã chốt.

### 3.3. Tách nội dung thành các phần có trách nhiệm rõ

`dữ kiện đã đọc → bảng theo chủ đề → kết luận được kiểm chứng → gợi ý câu hỏi`.

- Dựng bảng và lời dẫn ngắn theo cấu trúc trước, thay vì sinh một bài văn rồi xóa bảng/bullet bằng regex.
- Giữ `listing_table`, `nearby_table`, `response_tables` và renderer hợp đồng/lịch hẹn; tránh code lại bảng riêng từng nơi.
- Model có thể chọn các nhận xét ngắn từ proof đã kiểm tra. Không để model viết thêm một kết luận thứ hai cạnh kết luận của ứng dụng.
- Hỏi “Vì sao?” dùng lại mục tiêu/lựa chọn đã nêu, đọc lại Core và bảng so sánh ngắn; không âm thầm chọn phòng khác hoặc thêm lý do mới không cùng trọng tâm. Nếu facts đã đổi, báo thay đổi trước khi kết luận.
- Chỉ lưu reference, scope, objective, proof code và tham chiếu message cần thiết; không lưu thêm snapshot POI/hợp đồng thành nguồn sự thật lâu dài.

### 3.4. Gợi ý hỏi tiếp theo ngữ cảnh cho toàn bộ tool

Backend tạo tối đa bốn câu hỏi hoàn chỉnh, tự nhiên; frontend **bấm để điền input**, người dùng sửa/gửi chủ động. Không form, không dấu `[...]`, không tự gửi, không tự thực hiện thao tác.

Không dùng một bộ bốn câu cho mọi kết quả. Cũng không thay bằng một câu mẫu cứng cho mỗi keyword. Chọn từ **hành động tiếp theo mà nghiệp vụ hỗ trợ** rồi diễn đạt theo chủ đề, scope, phần đã trả lời, thông tin còn thiếu, khả năng tool và quyền người dùng.

Quy trình đề xuất:

1. Domain adapter tạo các hướng hỏi hợp lệ từ dữ kiện của lượt (không gọi API thêm chỉ để có chip).
2. Ưu tiên giải quyết dữ liệu thiếu quan trọng, bước nghiệp vụ tiếp theo, hoặc so sánh chưa trả lời.
3. Loại chủ đề vừa trả lời, câu lặp nghĩa trong vài lượt gần nhất, câu đã biết đáp án không đổi, hành động không được phép và “xem thêm” khi hết trang.
4. Nếu có model sinh sẵn câu trong bước đang chạy, kiểm tra câu đó thuộc hướng được phép, không bịa số tiền, địa danh, thời gian hay trạng thái. Không thêm một model call bắt buộc chỉ để viết chip. Có fallback ngắn theo intent/state.
5. Mỗi câu được chọn phải có route/tool hỗ trợ để khách bấm hỏi được thật. Không bịa tính năng, không gợi tự động ký/hủy/đặt lịch vì các tool hiện tại chỉ đọc/hướng dẫn.
6. Tập trung hoàn thiện gợi ý **sau** khi đã có kết quả của domain, bao gồm các nhánh trả sớm. Contract/Appointment không còn bị bỏ qua.

Ví dụ, không phải danh sách bắt buộc ở mọi lượt:

| Vừa trả lời | Hướng hỏi tiếp phù hợp | Hướng phải loại |
| --- | --- | --- |
| Tìm phòng | So sánh phí, Nearby, nhu cầu/đánh đổi, trang sau nếu còn | Đề nghị xem tiếp khi hết kết quả |
| So sánh phí | Phần cố định/tổng ước tính khi đủ context, chi phí nhận phòng, điều kiện thanh toán | Lặp “các khoản phí khác nhau thế nào?” ngay sau bảng phí |
| Nearby tổng quan | Đúng trường/nơi làm/siêu thị khách quan tâm; so sánh loại khác chưa xem | Lặp Nearby tổng quan, gợi quét TomTom, khẳng định gần trường khi chưa biết cơ sở |
| Giải thích lựa chọn | Kiểm tra đánh đổi, đối chiếu với đối thủ cụ thể, lịch xem của lựa chọn | Hỏi lại chính “vì sao bạn ưu tiên?” |
| Hợp đồng chờ ký | Ai cần ký tiếp, xem bản đang áp dụng, các bước ký theo quyền | Hoàn cọc như thể đã kết thúc hợp đồng, lời “ký giúp tôi” hứa thao tác |
| Hợp đồng đang thuê | Phí/hoá đơn, ngày thuê cuối, điều kiện hiện áp dụng | Gợi ký lại khi đã đủ chữ ký |
| Lịch hẹn chờ xác nhận | Trạng thái phản hồi, khung giờ hiện tại, cách đổi/hủy nếu được phép | “Đã xác nhận” khi chưa được chủ nhà duyệt |
| Lịch có đề xuất đổi giờ | So sánh giờ hiện tại và giờ đề xuất, bên cần phản hồi | Thay giờ hiện tại bằng giờ đề xuất chưa duyệt |
| Tool lỗi / cần đăng nhập | Thử đọc lại hoặc hướng dẫn đăng nhập | Chip hàm ý đã có dữ liệu, đã chọn phòng hay đã đặt lịch |

API public tiếp tục dùng `suggestedQuestions: string[]` hiện tại. Nếu cần metadata nội bộ để lọc trùng ý/ngữ cảnh qua reload, lưu mã chủ đề/tham chiếu nhỏ cùng message và có default cho hội thoại cũ; không bắt reset DB.

### 3.5. Trạng thái nghiệp vụ phải độc lập với lời văn

- Hợp đồng/lịch hẹn tiếp tục do Core quyết định trạng thái, quyền, thời gian; không để prompt hoặc chip thay đổi nghiệp vụ.
- Không trộn dữ kiện tin đăng hiện tại với điều khoản hợp đồng đã ký. Câu hỏi phí khi đang xem hợp đồng phải đọc contract terms/financial, không lấy bảng giá listing mới.
- Giữ router Appointment → Contract → Listing → RAG, nhưng kiểm thử chuyển chủ đề và câu nhập từ chip để tránh module trước chiếm câu hỏi sau. Không xây router mới nếu adapter/ngữ cảnh đã đủ xử lý.
- Lịch xem đọc availability trước khi gợi thời điểm; slot có thể gửi yêu cầu không đồng nghĩa đã đặt thành công. Chat hiện không thực hiện create/approve/cancel/reschedule/sign/payment.
- Phần review yêu cầu thuê là API riêng (`/rental-requests/review/ask`), chưa có `suggestedQuestions`. Nếu đưa vào phạm vi triển khai, thêm adapter/field tương thích cho màn review; không giả định nó đã đi qua `/agent/ask`.

## 4. Kế hoạch triển khai theo thứ tự

| Bước | Công việc cụ thể | File/module chính | Điều kiện hoàn tất |
| --- | --- | --- | --- |
| P1 | Chốt fixture bốn phòng của lỗi và gắn test thất bại cho đúng trọng tâm, trùng lý do, thứ tự bảng/kết luận và chip | Bộ case đi kèm; unit tests mới | Tái hiện F01–F07 mà không cần model thật hoặc TomTom |
| P2 | Bổ sung answer context; chuẩn hóa evidence phí và cơ sở tính. Chốt tái sử dụng Core/projection cần thiết; kiểm tra quyền đọc | `listing_ask`, `listing_value_advice`, `listing_table`, `tools/listings`; Core computed/calculator/DTO nếu thiếu | Phân biệt rent/fixed/estimated/unknown; không tự tạo context; scope không thay đổi |
| P3 | Focus và bằng chứng cho lựa chọn/đồng hạng; các phương án theo ưu tiên; lưu ngữ cảnh để giải thích | `listing_value_advice`, `listing_recommendation_explanation`, `renter_profile_advice`, schema generation | Phí xe chung không làm lý do chọn duy nhất; mọi ID/proof trong scope, không bắt ép đề xuất |
| P4 | Render bảng đúng chủ đề, kết luận phía dưới, bỏ văn lặp; sửa prompt theo structured output thay vì thêm regex chữa câu | `listing_ask._present`, `listing_table`, `response_tables`, `generative` | Các câu phí/Nearby/giải thích/profile đều theo cấu trúc đã duyệt |
| P5 | Gợi ý động cho listing và adapter chung cho Contract/Appointment/Knowledge; trạng thái/error/paging/role | `renter_guidance`, `agent_ask`, các use case; FE chỉ nếu cần thích nghi | Chip thay đổi theo chủ đề, không lặp điều đã trả lời, bấm chỉ set input |
| P6 | Tích hợp với memory, guest, reload và concurrency; cập nhật docs cũ/corpus; nghiệm thu đa lượt | `conversation_listing_memory`, repo/schema, guest state, frontend tests, docs | Đúng scope/ordinal/persona qua chuyển module; hội thoại cũ vẫn đọc được |
| P7 | Chạy test offline, Core integration nếu đổi phép tính, real model/readonly Core có log chấm từng case; UAT trên web | Bộ test và báo cáo kết quả | Đạt tiêu chí phần 6, không gọi TomTom hay ghi nghiệp vụ |

Không đổi seed, địa chỉ, Nearby sync, quota, DB schema listing hoặc thêm file SQL trong đợt này. Các thay đổi Core chỉ phục vụ projection tính phí nếu đã chứng minh tool hiện tại thiếu.

## 5. Kiểm thử và cách đánh giá

- **Unit/fixture:** kiểm tra planner contract, focus, phép so sánh, trùng lý do, định dạng, giới hạn chữ, chip theo state. Không chỉ kiểm tra model trả đúng shape JSON hoặc prompt có một câu quy tắc.
- **Integration offline:** chạy use case nhiều lượt với tool/model giả được kiểm soát; cố tình cho model chọn field không liên quan, ID ngoài scope hoặc bằng chứng bằng nhau để ứng dụng chặn.
- **Core tests:** nếu mở rộng computed projection, đối chiếu giá trị độc lập với SQL/calculator; kiểm tra thiếu số xe, đơn vị khác nhau, included/free/unknown, scope và precision. Không dùng DB dev để chạy test create-drop.
- **Real model:** dùng nhiều diễn đạt tự nhiên của cùng intent và chuỗi hội thoại; đánh giá nguyên cả pipeline thay vì chỉ parser. Ghi request ID, model, thời điểm, tool/arguments, focus, proof, ID và chip. Không ghi token/bí mật/nội dung riêng tư không cần thiết.
- **Frontend:** giữ GFM table/link; bấm chip điền input không gửi; chip đúng message mới nhất, đúng khi reload và khi có lượt đang chờ; kiểm tra desktop/mobile.
- **Chỉ đọc khi smoke test:** không append vào hội thoại khách, không `/agent/ask` ghi Mongo trên tài khoản thật; dùng use case trực tiếp với history fixture và readonly Core. Không provider scan, không TomTom.
- **Lỗi hoặc thiếu fixture:** báo `NOT_RUN`, `MISSING_FIXTURE`, `TOOL_UNAVAILABLE` riêng. Không coi rỗng là lỗi mạng, không gọi thiếu fixture là test pass.

## 6. Tiêu chí chấp nhận

1. Câu so sánh phí không còn đoạn kể lại bốn phòng trước bảng; kết luận nằm dưới và liên quan trực tiếp đến phí.
2. Không đề xuất riêng một phòng chỉ bằng một lợi ích mà các đối thủ cạnh tranh cũng có; không dùng lợi ích chung cả nhóm làm lý do thắng.
3. Không có lý do đã kiểm chứng thì không hiển thị một lựa chọn giả. Có thể đồng hạng và nêu đánh đổi ngắn.
4. Khi phù hợp, trả lời thẳng các phương án tiết kiệm/không gian/tiện nghi đã có bằng chứng, không lặp câu hỏi ba lựa chọn cứng. Chỉ hỏi thiếu dữ liệu thật sự ảnh hưởng kết quả.
5. Hỏi phí, Nearby, profile, lý do chọn và hợp đồng/lịch hẹn đều dùng đúng nguồn; mọi bảng nhiều kết quả giữ đúng tham chiếu.
6. Gợi ý dưới chat được chọn theo intent + kết quả + phần chưa trả lời + trạng thái nghiệp vụ, không cùng bộ mặc định cho mọi câu. Mỗi chip bấm hỏi phải chạy đúng một use case hỗ trợ.
7. Không gọi boundary cho follow-up cùng scope; không gọi TomTom trong chat; không thêm API call vô ích để tạo gợi ý.
8. Giữ quyền sở hữu dữ liệu, chỉ đọc tool nghiệp vụ, không lẫn lịch hiện tại/giờ đề xuất hay hợp đồng nháp/đã hiệu lực.
9. Tất cả case P0 trong tài liệu đi kèm đạt; chạy regression toàn bộ và đánh dấu rõ các case chưa kiểm thử model thật. Một số lượng unit test lớn không thay thế nghiệm thu chất lượng hội thoại.

## 7. Phạm vi xác nhận

Kế hoạch ưu tiên **so sánh phí và cơ sở lựa chọn trước**, sau đó dùng cùng cơ chế cho gợi ý đa nghiệp vụ. Không cam kết một ngưỡng “thông minh” chung chung: chất lượng được đánh giá bằng focus đúng, số liệu có căn cứ, lý do phân biệt được lựa chọn, phản hồi gọn và hội thoại tiếp nối đúng qua bộ case.

Runtime đã được triển khai sau khi người dùng duyệt. Không đổi schema/seed; Core có thêm projection phí theo shortlist. Việc nghiệm thu toàn bộ 86 case trên dữ liệu và giao diện thật vẫn phải ghi kết quả riêng, không suy ra từ tổng số unit test.
