# Chất lượng hội thoại AI — bàn giao triển khai

Ngày 01/11/2026. Kế hoạch: [audit](AI_CONVERSATION_QUALITY_AUDIT_PLAN.md). Bộ 86 case và năm hội thoại: [acceptance](AI_CONVERSATION_QUALITY_ACCEPTANCE_CASES.md).

## Hành vi đã thay đổi

| Phần | Hành vi hiện tại | Source chính |
| --- | --- | --- |
| So sánh phí | Bảng đơn giá gọn trước, kết luận dưới; không tự tính/hiện tổng cố định, không hỏi thêm số xe để tính tổng. Hỏi chỉ đơn giá điện/nước thì chỉ hiện những khoản đó | `listing_ask._present`, `listing_table` |
| Cơ sở chi phí | Chỉ dùng tiền thuê và từng khoản phí được công bố để đối chiếu. Không gọi projection tổng phí trong phần so sánh hoặc giải thích lựa chọn | `listing_value_advice`, `listing_ask` |
| Chọn trong nhóm | Lý do phải đúng chủ đề và tạo khác biệt với các đối thủ. Miễn phí xe chung của P2/P3/P4 không đủ để chọn riêng P3. Khi không đủ chứng cứ thì không chọn ép | `answer_context`, `listing_value_advice` |
| Các hướng ưu tiên | Cùng một lần đánh giá model có thể trả tối đa ba dòng: tiết kiệm, không gian, tiện nghi. Mỗi dòng được kiểm tra proof riêng; không bắt buộc đủ ba | `generate_value_advice`, `render_priorities` |
| Hỏi lý do | Giữ đúng ID và mục tiêu đã hiển thị; đọc lại dữ liệu và lập bảng đối chiếu. Lựa chọn lưu trong memory không được khác dòng ưu tiên đang hiển thị | `listing_recommendation_explanation`, `listing_ask._explain_chosen` |
| Tiếng Việt | Không coi “tiết kiệm” là lệnh “kiếm”. “Phòng nào đỡ tốn tiền hơn…” tiếp tục shortlist khi không có yêu cầu đổi vùng/query | `listing_context`, `listing_ask` |
| Bổ sung context | Câu khai số người, xe hoặc mức dùng mới đi qua planner/validation, không bị nhánh đọc chi tiết bỏ qua. Role không tự thành hard filter | `renter_guidance`, `renter_context_intake`, `listing_ask` |
| Gợi ý hỏi tiếp | Tối đa bốn câu theo domain, chủ đề vừa trả lời, scope, trạng thái và lịch sử gần đây. Loại lặp chủ đề, “xem thêm” khi hết trang, lời hứa thao tác chưa hỗ trợ | `conversation_guidance` và wrapper của Listing/Contract/Appointment |
| Memory | Lưu `answerContext` nhỏ trong message: domain/intent/topics/scope; giữ reference của các lựa chọn điều kiện. Hội thoại cũ không cần migration | `conversation_repo`, `conversation_listing_memory`, `guest_agent` |
| Frontend | Giữ API `suggestedQuestions: string[]`. Dùng lại component hiện có; bấm chip chỉ điền input, khách tự sửa/gửi | `hs-web-app/components/chat/AiChatWindow.tsx` |
| Nearby tổng quan | Chỉ liệt kê bảng tiện ích, không chạy model chọn phòng, không chèn nhận xét thiếu khác biệt hay tự hỏi mục tiêu. Khi khách yêu cầu chọn phòng theo Nearby mới đánh giá | `listing_ask._advise_main` |

Nearby vẫn đọc kho đã lưu. Follow-up không gọi boundary lại, không quét TomTom. Không đổi nghiệp vụ ký, cọc, đặt/hủy lịch hoặc quyền truy cập; tool contract/appointment vẫn chỉ đọc và hướng dẫn. Màn rental-request review là API riêng, không thêm trường/chip mới trong đợt này.

## Projection phí ở Core — không còn gọi tự động trong chat

Theo yêu cầu điều chỉnh sau khi thử giao diện, chat so sánh phí và giải thích lựa chọn không dùng endpoint bên dưới. Không render các dòng tổng kể cả khi payload cũ có trường projection. Core vẫn giữ API độc lập và công thức search hiện hữu; chúng không phải bước bắt buộc của phần so sánh phí.

`POST /api/v1/public/listings/ai/costs`

```json
{
  "listingIds": ["UUID của tin public trong shortlist"],
  "context": {"occupants": 3, "motorbikes": 1}
}
```

- Một batch có giới hạn như search, UUID duy nhất, chỉ tin public/active/chưa hết hạn. Không trả toàn bộ các tin của phường.
- Dùng chung biểu thức `Computed.RENT/FIXED_ROW/FIXED_TOTAL/ESTIMATED_TOTAL` với search; không sao chép công thức tiền sang model/Python.
- Mỗi item có `monthlyRent`, `knownFixedMonthly`, `fixedMonthlyTotal`, `estimatedMonthlyTotal`, `missingInputs`, `motorbikesAllowed`, `carsAllowed`. Giá trị chưa tính được là `null`.
- Không có context thì không gán mặc định. Chuẩn hóa giá theo người bằng số người thực tế, kiểm tra sức nhận xe, bỏ khoản đã gồm/không áp dụng theo cùng quy tắc search.
- Một lượt so phí không gọi batch projection này. Lượt “vì sao” đọc lại đơn giá/đặc điểm, không tìm tập tin mới hoặc gọi tính tổng.
- Gợi ý hỏi tiếp sau chủ đề phí không có câu tính tổng cố định hoặc câu hỏi Nearby. Khách chủ động hỏi Nearby vẫn đọc kho tiện ích đã lưu.

## Kết quả kiểm thử thực tế

| Kiểm tra | Kết quả | Phạm vi và giới hạn |
| --- | --- | --- |
| AI unit + corpus validation | **904 passed**, chạy lại sau điều chỉnh bỏ tính tổng/tự khuyên ở Nearby | `uv run --no-sync pytest tests/unit tests/eval/test_corpus.py -q`; corpus validation không phải chạy toàn bộ corpus bằng model thật |
| Core | **36 passed** | Search service 8, Search V2 15, calculator 12, cost SQL integration 1; integration có nhiều assertion về F4, context, precision, scope và xe |
| Build API | **PASS** | `mvn -q -Pisolated-build -pl hs-api-service -am compile -DskipTests` |
| Frontend chat | **24 passed** | `node --test tests/ai-suggested-questions.test.cjs tests/home-ai-chat.test.cjs`; gồm điền input/chủ động gửi, đổi chat, hiển thị/reload câu gợi ý |
| Model thật | **3 cách hỏi phí PASS**, thêm **2 lượt hỏi lý do PASS** | `gpt-4.1-mini-2025-04-14`, 5 call trong lần chạy cuối; F4 tổng hợp và tool Core giả, không phải dữ liệu khách thật |
| TomTom/ghi nghiệp vụ trong smoke | **0** | Không quét maps, không ghi Mongo/hợp đồng/lịch/thanh toán; không dùng `/agent/ask` trên tài khoản thật |
| PostgreSQL integration | **PASS**, DB tách biệt | Container tạm `hs_ai_search_test`, schema ngẫu nhiên; container đã dừng. Không chạy create/drop trên DB dev |
| Web UAT bằng browser trên dữ liệu thật | **NOT_RUN** | Chưa chụp ảnh xác nhận từng trạng thái/hội thoại thật sau restart |
| Toàn bộ 86 case + FLOW-A…E với model/dữ liệu thật | **NOT_RUN đầy đủ** | Không đánh đồng với tổng unit test. Các fixture quyền/đổi trạng thái/stream trả chậm cần chạy theo bộ acceptance |

Kết quả smoke và câu trả lời thực tế: [JSON](../tests/eval/results/conversation_quality_smoke_20261101.json). Script chạy lại có opt-in: `uv run python scripts/smoke_conversation_quality.py --live-model --output <report.json>`. Script chỉ dùng fixture tổng hợp, không lấy POI giả làm dữ liệu thật.

### Nơi kiểm tra từng nhóm lỗi

| Nhóm case | Test liên quan | Mức kiểm tra |
| --- | --- | --- |
| COST01–04, 16, 18–20, 22; NEXT01–06, 13, 15–16 | `test_conversation_quality.py`, `test_listing_value_advice.py`, `test_recommendation_privacy_and_language.py` | Bảng trước, proof cạnh tranh, bỏ field không liên quan, không ép chọn, đồng nhất lựa chọn hiển thị/memory, chip theo domain/state; không thay thế UAT từng diễn đạt |
| COST05–12, 14, 19; thêm số người/xe/mức dùng | `test_conversation_quality.py`, Core `AiListingCostsPostgresTest`, `RentalCostCalculatorTest` | Tổng đúng F4, null/partial, chuẩn hóa giá, xe vượt sức nhận, payload lỗi, đơn giá riêng; các tổ hợp custom fee thực tế vẫn cần UAT |
| ROOM01–16 | `test_nearby_recommendation_focus.py`, `test_nearby_followup_workflow.py`, `test_renter_profile_advice.py`, `test_renter_context_consultation.py`, `test_listing_value_advice.py` | Đúng nguồn/role/scope/proof, partial không thành vắng tiện ích, không gọi boundary follow-up |
| NEXT05, 08–12 | `test_listing_page_navigation.py`, `test_renter_profile_memory.py`, `test_agent_listing_route.py`, `test_conversation_repo.py`, test guest và frontend chat | Paging, lịch sử/state, API và persistence guards; chưa UAT mọi cuộc đua request với browser thật |
| CONTRACT01–10, APPOINT01–10 và chuyển domain | `test_contract_ask.py`, `test_appointment_ask.py`, `test_multi_result_tables.py`, `test_conversation_quality.py` | Tool/nguồn/quyền/routing/bảng hiện hữu và chip mới; không ghi nghiệp vụ |

## Cách chạy sau khi nhận code

1. **Restart AI service**, tải lại trang chat để nạp quy tắc mới. Không cần thay Core API cho lần điều chỉnh này.
2. Giữ cấu hình API key hiện có; không thêm biến môi trường.
3. Không drop DB, không chạy seed hay SQL migration cho thay đổi này. Hội thoại cũ vẫn dùng được.
4. Thử nhóm phòng → bảng đơn giá phí → ưu tiên không gian → “Vì sao?” → Nearby. Xác nhận phần phí không hiện tổng, không hỏi số xe để tính, và Nearby tổng quan không tự chọn phòng.

Các test cũ buộc phải chọn một ID dù không có lợi thế đã được cập nhật theo yêu cầu mới. Test vẫn chặn ID ngoài scope và dữ kiện do model tự bịa; không nới những chốt kiểm tra đó để làm xanh bộ test.
