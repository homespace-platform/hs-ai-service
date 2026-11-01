# Xoá lịch sử hội thoại AI

- Frontend/Gateway: `DELETE /api/v1/ai/agent/conversations`.
- AI service: `DELETE /agent/conversations`.
- Yêu cầu đăng nhập; tài khoản lấy từ header định danh đáng tin cậy do Gateway cấp. Không nhận `ownerId` từ người gọi để chọn tài khoản khác.
- Trả `204 No Content`, kể cả khi lịch sử đã trống.
- Xoá vật lý bằng `delete_many({ownerId: current_user_id})` trên collection MongoDB `ai_conversations`. Mỗi document chứa cả tin nhắn, trạng thái tìm phòng và ghim; không dùng cờ ẩn hoặc soft delete.
- Không xoá tin nhắn giữa khách/chủ nhà, hồ sơ tài khoản hay dữ liệu listing.

Ở `/chat`, kênh **Trợ lý AI** có nút **Xoá tất cả hội thoại** dưới **Đoạn chat mới**. Xác nhận xoá sẽ áp dụng cho cả hội thoại đã ghim. Khi thao tác thành công, sidebar, nội dung đang xem và bản nháp được xoá khỏi giao diện; chỉ tạo document mới khi bắt đầu hội thoại tiếp theo. Nếu API lỗi, giao diện giữ nguyên lịch sử để thử lại.

Kiểm thử: `tests/unit/test_bulk_conversation_delete.py`; bật `HS_RUN_MONGO_INTEGRATION=1` để chạy `tests/contract/test_conversation_history.py::test_real_mongo_bulk_delete_removes_messages_memory_and_pins_only_for_current_owner`. Bài kiểm thử Mongo tạo hai tài khoản UUID riêng và chỉ dọn dữ liệu thử nghiệm của chúng.
