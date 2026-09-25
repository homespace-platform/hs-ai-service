# HomeSpace AI Service

FastAPI microservice for HomeSpace AI capabilities, local RAG knowledge base, and platform agent integration:

- Listens on port `8084` in local development.
- Registers as `HS-AI-SERVICE` with Eureka at `http://localhost:8761/eureka`.
- Is reached by client applications through API Gateway:
  - User Q&A: `/api/v1/ai/**`
  - Admin Knowledge Base: `/api/v1/admin/ai/**` (strictly authorized by `GatewayRouteAuthorization` for role `ADMIN`).
- Validates internal Gateway secret `X-Internal-Secret` to reject untrusted direct traffic on port 8084.
- Storage: Dedicated PostgreSQL instance with `pgvector` extension running on port `5433`.
- Local Embedding: `intfloat/multilingual-e5-small` via `sentence-transformers` running on CPU (384-dimensional vector, normalized).
- Generative Layer: Gemini / Groq / Disabled mode for synthesis with source citations.

---

## 1. Quick Start Locally

### Step 1: Start PostgreSQL AI (pgvector)

In `hs-infrastructure`, the `postgres-ai` container runs `pgvector/pgvector:pg16` on host port `5433`:

```powershell
cd D:\Workspace\homespace\hs-infrastructure
docker compose -f docker-infrastructure.yml up -d postgres-ai
```

> **Kiểm tra cổng 5433:**
>
> ```powershell
> Get-NetTCPConnection -LocalPort 5433 -ErrorAction SilentlyContinue
> ```
>
> Nếu cổng 5433 đã bị chiếm bởi tiến trình khác, bạn có thể đổi port mapping trong `docker-infrastructure.yml` (ví dụ `5434:5432`) và cập nhật `AI_DATABASE_URL` trong `.env` tương ứng.

### Step 2: Install Dependencies & Run Database Migrations

In `hs-ai-service`:

```powershell
cd D:\Workspace\homespace\hs-ai-service
Copy-Item .env.example .env
uv sync
uv run alembic upgrade head
```

Generate a private Gateway trust value, then set the **same** value as
`GATEWAY_INTERNAL_SECRET` in `hs-ai-service/.env` and
`hs-gateway-server/.env.dev` before using authenticated AI endpoints:

```powershell
[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

The old example value `hs-internal-gateway-secret-dev-2026` is rejected. Do
not commit either real `.env` file. Restart Gateway and AI Service after the
change. If `GENERATION_PROVIDER=disabled`, retrieval still works but `/agent/ask`
returns `GENERATION_UNAVAILABLE` with citations instead of pretending an
excerpt is a generated answer. Set a supported `GENERATION_PROVIDER` and its
matching `GENERATION_MODEL` when you are ready to use Gemini or Groq.

To fail over automatically from Gemini to Groq on rate limits, timeouts,
network failures, or provider 5xx errors, configure:

```dotenv
GENERATION_PROVIDER=gemini
GENERATION_FALLBACK_PROVIDER=groq
GEMINI_MODEL=gemini-2.5-flash
GROQ_MODEL=openai/gpt-oss-20b
```

### Step 3: Run Ingestion Worker (Terminal 2)

The worker processes asynchronous embedding jobs safely with lease-based locking and atomic version activation:

```powershell
cd D:\Workspace\homespace\hs-ai-service
uv run python -m homespace_ai.knowledge.ingestion.worker
```

### Step 4: Start AI Web API (Terminal 1)

```powershell
cd D:\Workspace\homespace\hs-ai-service
uv run uvicorn homespace_ai.main:app --app-dir src --host 0.0.0.0 --port 8084 --reload
```

---

## 2. Ingest Sample Knowledge Documents

Run the idempotent CLI script to import all `.md` files from `hs-infrastructure/prompts/knowledge` into the database as `DRAFT` status:

```powershell
uv run python -m homespace_ai.knowledge.ingestion.cli_import --dir ../hs-infrastructure/prompts/knowledge
```

Re-running this command is completely idempotent: files with matching content SHA-256 and configuration versions are recognized as `UNCHANGED` and will not create duplicate versions or duplicate jobs.
If documents were imported before the embedding model revision was pinned,
run the import again. It creates a new DRAFT version with the pinned model
identity; an admin must still approve and publish it explicitly.

---

## 3. Endpoints & Gateway URL Mapping

Gateway automatically strips route prefixes:

- Admin calls enter Gateway at `/api/v1/admin/ai/...` and are rewritten to `/admin/...` at FastAPI.
- User calls enter Gateway at `/api/v1/ai/...` and are rewritten to `/...` at FastAPI.

### Admin Knowledge Endpoints (Requires role `ADMIN`)

| Purpose                    | Gateway Path                                        | Service Internal Path                     | Method             |
| -------------------------- | --------------------------------------------------- | ----------------------------------------- | ------------------ |
| Upload Markdown document   | `/api/v1/admin/ai/knowledge/documents`              | `/admin/knowledge/documents`              | `POST` (multipart) |
| List documents (paginated) | `/api/v1/admin/ai/knowledge/documents`              | `/admin/knowledge/documents`              | `GET`              |
| Document detail & versions | `/api/v1/admin/ai/knowledge/documents/{id}`         | `/admin/knowledge/documents/{id}`         | `GET`              |
| Patch draft metadata       | `/api/v1/admin/ai/knowledge/documents/{id}`         | `/admin/knowledge/documents/{id}`         | `PATCH`            |
| Approve document           | `/api/v1/admin/ai/knowledge/documents/{id}/approve` | `/admin/knowledge/documents/{id}/approve` | `POST`             |
| Publish (enqueue job)      | `/api/v1/admin/ai/knowledge/documents/{id}/publish` | `/admin/knowledge/documents/{id}/publish` | `POST` (202)       |
| Reindex active version     | `/api/v1/admin/ai/knowledge/documents/{id}/reindex` | `/admin/knowledge/documents/{id}/reindex` | `POST` (202)       |
| Ingestion job status       | `/api/v1/admin/ai/knowledge/jobs/{jobId}`           | `/admin/knowledge/jobs/{jobId}`           | `GET`              |
| Archive document           | `/api/v1/admin/ai/knowledge/documents/{id}/archive` | `/admin/knowledge/documents/{id}/archive` | `POST`             |
| Diagnostic vector search   | `/api/v1/admin/ai/knowledge/search`                 | `/admin/knowledge/search`                 | `POST`             |

### User Endpoints (Authenticated)

| Purpose                     | Gateway Path              | Service Internal Path | Method |
| --------------------------- | ------------------------- | --------------------- | ------ |
| Ask AI agent (RAG Q&A)      | `/api/v1/ai/agent/ask`    | `/agent/ask`          | `POST` |
| Gateway identity diagnostic | `/api/v1/ai/auth/me`      | `/auth/me`            | `GET`  |
| Ping                        | `/api/v1/ai/ping`         | `/ping`               | `GET`  |
| Liveness Probe              | `/api/v1/ai/health/live`  | `/health/live`        | `GET`  |
| Readiness Probe             | `/api/v1/ai/health/ready` | `/health/ready`       | `GET`  |

### Frontend entry points

- Web app: `http://localhost:3000/chat?channel=ai` calls the Gateway ask API.
  AI sessions are currently kept in browser memory only; the AI Service does
  not persist conversation history yet.
- Admin portal: `http://localhost:5000/operations/knowledge` supports Markdown
  upload, draft metadata, approval, publishing, job status, reindex, archive,
  and diagnostic retrieval. All actions use Gateway URLs and the logged-in
  admin's bearer token.
- All existing sample Markdown files are drafts. Unless an admin explicitly
  approves and publishes them, the user chat will return `NO_EVIDENCE`.

---

## 4. Model Caching & Offline Operation

- The local embedding model `intfloat/multilingual-e5-small` (~470 MB) is downloaded on first use and permanently saved to `MODEL_CACHE_DIR` (`./models_cache`).
- Subsequent restarts and queries run 100% offline from the local cache on CPU without making external network calls.
- Chunks are prefixed with `passage: ` and user questions are prefixed with `query: ` as specified by the E5 model family.

---

## 5. Security & Verification

1. **Gateway Internal Token (`X-Internal-Secret`)**:
   Gateway strips incoming client headers and attaches a trusted internal secret. Any direct requests to port 8084 lacking this secret are rejected with `403 Forbidden`.
2. **Access Control (ACL)**:
   Documents with `visibility: admin` are filtered out at the SQL query level before cosine similarity computation and are never included in context prompts or citations for non-admin users.
3. **Prompt Injection Defense**:
   Knowledge chunks are treated as untrusted text data. The system prompt instructs the model to ignore any instructions inside document text attempting to override system behavior.
4. **Realtime / Personal Data Detection**:
   Queries asking for personal state (current rent amount, listing approval status, contract signing state) are classified as `OUT_OF_SCOPE` without querying static documentation.

---

## 6. Testing & Quality Benchmarks

Run unit and contract tests without changing a database:

```powershell
uv run pytest -v
```

Database integration tests are deliberately skipped in the default run. To run
them, point `AI_DATABASE_URL` at a **dedicated database whose name ends in
`_test`**, apply migrations there, and set `HS_RUN_RAG_INTEGRATION=1`.
The Vietnamese benchmark additionally needs `HS_RUN_RAG_BENCHMARK=1` and
approved sample knowledge in that isolated database. Never point the benchmark
at a development/production database: the original test approves draft records.

```
Set-Alias uv "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe"

ter1:
uv run uvicorn homespace_ai.main:app --app-dir src --host 0.0.0.0 --port 8084 --reload

ter2:
uv run alembic upgrade head
uv run python -m homespace_ai.knowledge.ingestion.worker
```
