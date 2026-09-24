# Thiết lập Python và FastAPI cho HomeSpace AI Service

Tài liệu này ghi lại quá trình thiết lập môi trường Python cho `hs-ai-service` trên Windows bằng PowerShell và `uv`.

## Môi trường hiện tại

- Hệ điều hành: Windows
- Shell: PowerShell 7.6.6
- Python: 3.12.10
- uv: 0.12.18
- FastAPI: 0.141.1
- FastAPI CLI: 0.0.32
- Thư mục dự án: `D:\Workspace\homespace\hs-ai-service`

## 1. Cài đặt Python 3.12

Trên máy hiện tại, Python 3.12.10 đã được cài đặt sẵn. Trên một máy Windows mới, có thể cài Python 3.12 bằng `winget`:

```powershell
winget install --id Python.Python.3.12 -e
```

Đóng và mở lại PowerShell, sau đó kiểm tra:

```powershell
python --version
```

Kết quả trên máy hiện tại:

```text
Python 3.12.10
```

## 2. Cài đặt uv

`uv` được sử dụng để quản lý phiên bản Python, virtual environment và dependency của dự án.

```powershell
winget install --id=astral-sh.uv -e
```

Sau khi cài đặt, cần đóng terminal hiện tại và mở terminal mới để Windows nạp lại biến `PATH`:

```powershell
uv --version
```

Kết quả hiện tại:

```text
uv 0.12.18
```

Nếu chưa muốn mở terminal mới, có thể nạp lại `PATH` ngay trong PowerShell hiện tại:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
uv --version
```

## 3. Khởi tạo dự án

Di chuyển tới workspace và khởi tạo service:

```powershell
cd D:\Workspace\homespace
uv init hs-ai-service --bare
cd hs-ai-service
```

Kết quả:

```text
Initialized project `hs-ai-service` at `D:\Workspace\homespace\hs-ai-service`
```

Lệnh `--bare` chỉ tạo cấu hình dự án tối thiểu, không sinh file ứng dụng mẫu.

## 4. Cố định phiên bản Python

```powershell
uv python pin 3.12
```

Lệnh này tạo file `.python-version` với nội dung `3.12`. Khi cài dependency, `uv` đã chọn Python hiện có tại:

```text
C:\Users\nvmin\AppData\Local\Programs\Python\Python312\python3.exe
```

## 5. Cài đặt FastAPI

```powershell
uv add "fastapi[standard-no-fastapi-cloud-cli]"
```

Lệnh trên đồng thời:

- Tạo virtual environment tại `.venv`.
- Thêm FastAPI vào `pyproject.toml`.
- Cài FastAPI CLI, Uvicorn, Pydantic, HTTPX, WebSocket và các dependency tiêu chuẩn.
- Tạo hoặc cập nhật file khóa phiên bản `uv.lock`.

## 6. Cài đặt các thư viện nền tảng

```powershell
uv add pydantic-settings httpx structlog tenacity
```

Mục đích của các dependency trực tiếp:

- `pydantic-settings`: đọc và kiểm tra cấu hình từ biến môi trường hoặc file `.env`.
- `httpx`: gọi bất đồng bộ tới Core API, Chat Service, News Service và Storage Service.
- `structlog`: tạo log có cấu trúc.
- `tenacity`: retry có kiểm soát khi gọi service hoặc AI provider.

`pydantic-settings` và `httpx` đã được FastAPI cài trước đó, vì vậy lệnh này chỉ cần tải thêm `structlog` và `tenacity`, đồng thời ghi cả bốn thư viện thành dependency trực tiếp của dự án.

## 7. Kiểm tra môi trường

Không cần kích hoạt `.venv` khi sử dụng `uv run`.

```powershell
uv run python --version
uv run fastapi --version
```

Kết quả hiện tại:

```text
Python 3.12.10
FastAPI CLI version: 0.0.32
```

## 8. Cài lại môi trường trên máy khác

Khi repository đã có `pyproject.toml` và `uv.lock`, không cần chạy lại từng lệnh `uv add`. Chỉ cần:

```powershell
cd D:\Workspace\homespace\hs-ai-service
uv sync --frozen
```

Sau đó kiểm tra:

```powershell
uv run python --version
uv run fastapi --version
```

`uv sync --frozen` tạo `.venv` và cài đúng các phiên bản đã khóa trong `uv.lock` mà không tự ý cập nhật dependency.

## 9. Cảnh báo hardlink trên máy hiện tại

Trong lúc cài package, `uv` hiển thị cảnh báo:

```text
warning: Failed to hardlink files; falling back to full copy.
```

Đây không phải lỗi cài đặt. Cache của `uv` đang nằm trên ổ `C:` trong khi dự án nằm trên ổ `D:`, nên Windows không thể tạo hardlink giữa hai ổ đĩa. `uv` đã tự động chuyển sang sao chép file và toàn bộ package vẫn được cài thành công.

Để ẩn cảnh báo trong PowerShell hiện tại, có thể đặt:

```powershell
$env:UV_LINK_MODE = 'copy'
```

Hoặc thêm `--link-mode=copy` khi chạy một lệnh cụ thể:

```powershell
uv sync --link-mode=copy
```

Việc này chỉ ảnh hưởng cách `uv` đưa package từ cache vào `.venv`, không ảnh hưởng chức năng của ứng dụng.

## 10. Khởi tạo khung HomeSpace AI Service

Service sử dụng `src` layout và chia phần HTTP, use case, domain, tích hợp ngoài và năng lực AI:

```text
src/homespace_ai/
|-- api/v1/                    # FastAPI routes
|-- application/use_cases/     # Điều phối use case
|-- domain/                    # Quy tắc nghiệp vụ AI
|-- clients/                   # Gọi API qua Gateway
|-- core/                      # Settings, logging, identity/security
|-- discovery/                 # Đăng ký và heartbeat Eureka
|-- knowledge/                 # RAG điều khoản/hướng dẫn hệ thống
|   |-- ingestion/
|   `-- retrieval/
|-- tools/                     # Lấy dữ liệu nghiệp vụ realtime
|-- property_search/           # Tìm kiếm tài sản bằng ngôn ngữ tự nhiên
|-- models/                    # Model/embedding/reranker adapters
|-- events/                    # Consumer Kafka khi có use case
`-- repositories/              # Lưu trữ dữ liệu riêng của AI

tests/
|-- unit/
|-- integration/
`-- contract/
```

Các package tương lai hiện có `__init__.py` để giữ cấu trúc trong Git; chỉ thêm dependency và code khi có use case cụ thể.

## 11. Cấu hình môi trường và cổng

Sao chép file mẫu thành cấu hình local:

```powershell
Copy-Item .env.example .env
```

Các thiết lập mặc định:

```dotenv
APP_NAME=hs-ai-service
HOST=0.0.0.0
PORT=8084
EUREKA_CLIENT_SERVICE_URL=http://localhost:8761/eureka
EUREKA_INSTANCE_HOSTNAME=localhost
GATEWAY_BASE_URL=http://localhost:8080
```

Service tự dò địa chỉ IPv4 dùng cho kết nối đi khi đăng ký Eureka. Nếu instance hiện IP không truy cập được từ Gateway, đặt địa chỉ host/IP có thể truy cập được tại `EUREKA_INSTANCE_HOSTNAME` trong file `.env`.

## 12. Chạy service và đường dẫn API

```powershell
uv sync --frozen
uv run uvicorn homespace_ai.main:app --app-dir src --host 0.0.0.0 --port 8084 --reload
```

| Kiểm tra | URL trực tiếp | URL qua Gateway |
|---|---|---|
| Liveness | `http://localhost:8084/health/live` | `/api/v1/ai/health/live` |
| Readiness | `http://localhost:8084/health/ready` | `/api/v1/ai/health/ready` |
| Ping Eureka | `http://localhost:8084/ping` | `GET /api/v1/ai/ping` |
| Swagger | `http://localhost:8084/docs` | Requires JWT through Gateway |
| Kiểm tra identity | `http://localhost:8084/auth/me` | `GET /api/v1/ai/auth/me` |

Gateway cần được khởi động lại để nạp route `/api/v1/ai/**` mới. Endpoint `/auth/me` cần JWT hợp lệ; Gateway chuyển thành `X-User-Id`, `X-User-Email`, `X-User-Name`, `X-User-Name-B64`, `X-User-Role`, `X-User-Authorities`. Service chỉ tin các header này khi request đi qua Gateway. Response thành công giữ format `{ "code": 1000, "result": ... }` của NestJS; `/ping` trả `"pong"`.

## Các file môi trường đã được tạo

```text
hs-ai-service/
|-- .python-version
|-- .venv/
|-- .env.example
|-- pyproject.toml
|-- uv.lock
|-- README.md
|-- docs/ARCHITECTURE.md
|-- src/homespace_ai/
`-- SETUP_PYTHON.md
```

- Commit `.python-version`, `.env.example`, `pyproject.toml`, `uv.lock` và tài liệu vào Git.
- Không commit thư mục `.venv`.
- Không commit file `.env`.
