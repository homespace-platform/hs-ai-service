```
Trợ lý tìm nhà: người thuê nói “dưới 6 triệu, gần IUH, có chỗ để xe máy”; AI chuyển thành bộ lọc, tìm trong tin đăng đang hiển thị, xếp hạng và giải thích vì sao từng căn phù hợp.
Trợ lý giao dịch cá nhân: trả lời “hợp đồng của tôi đang ở bước nào?”, “hóa đơn này gồm những khoản gì?”, “tôi cần làm gì tiếp?” bằng cách đọc API hợp đồng, hóa đơn, yêu cầu thuê — không dùng RAG tĩnh để đoán trạng thái.
Trợ lý đăng tin cho chủ nhà: soạn tiêu đề/mô tả từ dữ liệu căn nhà, phát hiện thông tin thiếu hoặc mâu thuẫn, gợi ý câu trả lời cho khách; chủ nhà duyệt trước khi đăng/gửi.
Trợ lý hồ sơ hợp đồng: tóm tắt điều khoản, so sánh bản sửa đổi, chỉ ra chỗ khác với thông tin đã thỏa thuận. Không để AI tự kết luận tính hợp pháp hay tự sửa bản đã ký.
Trợ lý vận hành: phân loại khiếu nại, tổng hợp lịch sử trao đổi, phát hiện hóa đơn bất thường (ví dụ điện tăng đột biến) và đưa việc cần kiểm tra cho đúng người.
```

Trong Software Engineering, nếu bạn đã hiểu về RAG (Retrieval-Augmented Generation) và đang tìm hiểu hệ thống AI có khả năng truy vấn CSDL (Database) theo thời gian thực để trả lời người dùng, thì công nghệ bạn đang tìm có thể là:

Text-to-SQL (NL2SQL) hoặc AI Agent kết hợp Database Tools (Database Agent).

Hai khái niệm này liên quan với nhau nhưng không hoàn toàn giống nhau.

## 1. Phân biệt các hệ thống AI

| Công nghệ                       | Chức năng chính                                                              |
| ------------------------------- | ---------------------------------------------------------------------------- |
| RAG                             | Tìm kiếm tài liệu, kiến thức rồi sử dụng LLM để trả lời                      |
| Text-to-SQL                     | Chuyển câu hỏi ngôn ngữ tự nhiên thành truy vấn SQL                          |
| Database Agent                  | AI sử dụng công cụ truy vấn database để lấy dữ liệu thực tế và trả lời       |
| AI Agent                        | AI có thể lập kế hoạch, gọi API, truy vấn CSDL và thực hiện nhiều bước xử lý |
| Tool Calling / Function Calling | Cho phép LLM gọi các hàm hoặc API mà backend cung cấp                        |

## 2. Ví dụ với hệ thống HomeSpace

Giả sử bạn đang xây dựng AI hỗ trợ người thuê nhà và chủ nhà.

Người dùng hỏi:

> "Tháng này tôi còn bao nhiêu hóa đơn chưa thanh toán?"

AI sẽ không chỉ tìm kiếm văn bản như RAG mà cần lấy dữ liệu thực tế trong CSDL.

Kiến trúc Database Agent

User

"Tôi còn bao nhiêu hóa đơn chưa thanh toán?"

LLM / AI Agent

Hiểu ý định và chọn công cụ

Backend API / Tool Calling

getUnpaidInvoices(userId)

Database (PostgreSQL / MySQL)

Trả về hóa đơn chưa thanh toán

AI trả lời người dùng

"Bạn có 2 hóa đơn chưa thanh toán, tổng cộng 3.200.000 VNĐ."

Dữ liệu này được lấy khi người dùng đặt câu hỏi, không phải từ kiến thức đã được AI ghi nhớ.

## 3. Nên dùng công nghệ nào trong dự án thực tế?

Với HomeSpace sử dụng Spring Boot và CSDL quan hệ, mình đề xuất:

- AI Agent + Function Calling: Phù hợp nhất khi AI cần xem hóa đơn, hợp đồng, trạng thái thanh toán, yêu cầu hỗ trợ và dữ liệu riêng của từng tài khoản.
- Text-to-SQL: Phù hợp khi người dùng hỏi câu phân tích linh hoạt như "Có bao nhiêu hợp đồng hết hạn trong 30 ngày tới?".
- RAG + Database Agent: Kết hợp cả kiến thức từ tài liệu và dữ liệu giao dịch thực tế.

Đặc biệt, với hệ thống production, không nên cho LLM tự do chạy SQL trên CSDL chính. Nên sử dụng backend API có xác thực, phân quyền và kiểm soát dữ liệu. Nếu dùng Text-to-SQL, hãy giới hạn quyền đọc và kiểm soát truy vấn.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://developers.openai.com\&sz=32)

OpenAI API

+1



## 4. Tài liệu để tìm hiểu

- [OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)
- [LangGraph SQL Agent](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/sql-agent.mdx)
- [LangChain SQL Database Toolkit](https://reference.langchain.com/python/langchain-community/agent_toolkits/sql/toolkit/SQLDatabaseToolkit)

Mình muốn phân biệt thêm một điểm: Bạn đang muốn xây dựng AI để trả lời câu hỏi dựa trên dữ liệu CSDL realtime, hay AI có thể thực hiện hành động trong hệ thống (ví dụ: tạo yêu cầu hỗ trợ, cập nhật hóa đơn, quản lý hợp đồng)?

Hai hướng này sẽ quyết định kiến trúc AI bạn nên xây dựng.
