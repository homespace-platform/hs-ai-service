# HomeSpace AI Service

FastAPI microservice for HomeSpace AI capabilities, local RAG knowledge base, and platform agent integration:

- Listens on port `8084` in local development.
- Registers as `HS-AI-SERVICE` with Eureka at `http://localhost:8761/eureka`.
- Is reached by client applications through API Gateway:
  - User Q&A: `/api/v1/ai/**`
  - Admin Knowledge Base: `/api/v1/admin/ai/**` (strictly authorized by `GatewayRouteAuthorization` for role `ADMIN`).
- Validates internal Gateway secret `X-Internal-Secret` to reject untrusted direct traffic on port 8084.
- Storage: Dedicated PostgreSQL instance with `pgvector` on port `5433`; AI conversation history uses the existing MongoDB container on port `27017` in its own `homespace_ai` database.
- Local Embedding: `intfloat/multilingual-e5-small` via `sentence-transformers` running on CPU (384-dimensional vector, normalized).
- Generative Layer: OpenAI Responses / Disabled mode for synthesis with source citations.

---

## 1. Quick Start Locally

### Step 1: Start PostgreSQL AI (pgvector)

In `hs-infrastructure`, the `postgres-ai` container runs `pgvector/pgvector:pg16` on host port `5433`:

```powershell
cd D:\Workspace\homespace\hs-infrastructure
docker compose -f docker-infrastructure.yml up -d postgres-ai mongodb
```

> **Kiểm tra cổng 5433:**
>
> ```powershell
> Get-NetTCPConnection -LocalPort 5433 -ErrorAction SilentlyContinue
> ```
>
> Nếu cổng 5433 đã bị chiếm bởi tiến trình khác, bạn có thể đổi port mapping trong `docker-infrastructure.yml` (ví dụ `5434:5432`) và cập nhật `AI_DATABASE_URL` trong `.env` tương ứng.

The AI service also needs `AI_MONGODB_URI` (see `.env.example`). Conversation endpoints
create their index automatically at startup. History is scoped to the authenticated
Gateway user ID; deleting a conversation removes its stored messages.

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
excerpt is a generated answer.

### OpenAI generation and model allocation

Set `GENERATION_PROVIDER=openai` and `OPENAI_API_KEY` in the ignored server-side
`.env`. Do not expose the key to the frontend, screenshots, logs, or Git.
Gemini/Groq clients and fallback configuration are no longer used.

| Task | Default model | Purpose |
| --- | --- | --- |
| RAG / admin general answers | gpt-4o-mini | Lower-cost answers from retrieved documents |
| Listing tool planning | gpt-4.1-mini | Strict native function calls using the Core field catalog |
| Listing answers / comparisons | gpt-4.1-mini | Ground answers in full live public Core evidence |

Each model can be changed independently via `OPENAI_RAG_MODEL`,
`OPENAI_TOOL_MODEL`, and `OPENAI_LISTING_MODEL`. GPT reasoning models use
`OPENAI_REASONING_EFFORT=low`; non-reasoning models use `GENERATION_TEMPERATURE`.
Models must support Responses API; the tool model must also support strict function calling.
The defaults above are non-reasoning models: no reasoning parameter is sent.
Local mocked tests verify routing and request compatibility, not live model answer quality.

Embedding remains local: `intfloat/multilingual-e5-small`. No re-ingestion or
OpenAI embedding cost is required. RAG retrieval and Core authorization are unchanged.
Core tool JSON is passed intact: no field filtering or payload compaction is introduced.
Provider context windows and account token/rate limits still apply.

Responses requests use `store=false` (this does not override OpenAI abuse-monitoring
retention). Logs contain task/model/token usage, including cached and reasoning tokens;
never prompts, tool result bodies, API keys or provider error bodies. Use these logs and
the OpenAI project usage dashboard to track spend. Prepaid credit can still run out.
No automatic upgrade to a more expensive model is enabled.

Output budgets (not input payload caps) default to 4096 tokens for RAG/planning and
16384 for listing answers. If an answer hits the output budget, retry once with twice
the budget (maximum 32768), never present a partial answer. Retries can incur extra
cost. HTTP 429/transient network/5xx failures are retried once; exhausted billing
quota, auth failures and invalid requests are not retried. Restart AI service after
changing `.env`; there is no frontend API-contract change.

Verification (unit tests do not call the paid API):

```powershell
uv run pytest tests/unit -q
# Opt-in: three paid smoke requests with synthetic data, no DB/Gateway calls.
uv run python scripts/smoke_openai.py --live
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

Use Admin at `http://localhost:5000/operations/knowledge` to drag/drop or select up to 50 Markdown files (1 MB each). Upload creates the next numeric version and queues a worker job, even when contents are unchanged. `README.md` is excluded. `visibility: admin` remains restricted to admins. You can also use the CLI:

```powershell
uv run python -m homespace_ai.knowledge.ingestion.cli_import --dir ../hs-infrastructure/documents/knowledge
```

Identity is the normalized filename (Unicode NFC, case-insensitive), not the front-matter document_id. Version numbers are server-managed: 1, 2, 3, ...; migration preserves existing document IDs. Do not re-import after every restart: PostgreSQL persists the documents and vectors.

Choose **Mở và chỉnh sửa Markdown**, edit the full Markdown with YAML metadata, and click **Cập nhật và áp dụng version mới**. Every save increments the version; a stale editor receives 409 rather than overwriting a newer update.

The old active version remains available until replacement succeeds. Activation and deletion of obsolete vector chunks happen in one transaction. Failed ingestion leaves the old vectors intact. Version text/metadata and jobs remain as audit history; retired versions are archived. An older worker cannot roll back a newer active version. Archived documents cannot be edited by this workflow.

Apply the additive migration, then restart both API and worker on the updated code:

```powershell
uv run alembic upgrade head
```

Legacy duplicate filenames produce a conflict rather than silently merging documents. Legacy approve/publish routes remain for old drafts and retrying failed ingestion; new uploads/edits do not need those manual steps.

---

## 3. Endpoints & Gateway URL Mapping

Gateway automatically strips route prefixes:

- Admin calls enter Gateway at `/api/v1/admin/ai/...` and are rewritten to `/admin/...` at FastAPI.
- User calls enter Gateway at `/api/v1/ai/...` and are rewritten to `/...` at FastAPI.

### Admin Knowledge Endpoints (Requires role `ADMIN`)

| Purpose                    | Gateway Path                                        | Service Internal Path                     | Method             |
| -------------------------- | --------------------------------------------------- | ----------------------------------------- | ------------------ |
| Upload Markdown document   | `/api/v1/admin/ai/knowledge/documents`              | `/admin/knowledge/documents`              | `POST` (multipart) |
| Upload multiple files      | `/api/v1/admin/ai/knowledge/documents/batch`        | `/admin/knowledge/documents/batch`        | `POST` (202, files[]) |
| Open editable Markdown     | `/api/v1/admin/ai/knowledge/documents/{id}/content` | `/admin/knowledge/documents/{id}/content` | `GET` |
| Save next Markdown version | `/api/v1/admin/ai/knowledge/documents/{id}/content` | `/admin/knowledge/documents/{id}/content` | `PUT` (202, markdown + expectedVersionId) |
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
  AI sessions are persisted in MongoDB, scoped to the authenticated owner.
- Admin portal: `http://localhost:5000/operations/knowledge` supports Markdown
  bulk upload, Markdown editing with automatic versioning/ingestion, job status, reindex, archive,
  and diagnostic retrieval. All actions use Gateway URLs and the logged-in
  admin's bearer token.
- Sample files are working drafts: Admin must review them before upload/apply. This action now explicitly queues ingestion automatically, irrespective of the file's `status` field.

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

Test managed versions and legacy migration in a new disposable PostgreSQL database (removed afterward; no OpenAI requests or embedding downloads):

```powershell
uv run python scripts/test_managed_knowledge.py
```

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

API and worker validate the RAG tables/columns at startup. If `worker_heartbeats`
or other knowledge tables are missing, startup stops with a migration instruction
instead of repeatedly failing or registering an unusable API in Eureka. Run the
migration in the service folder using the same `AI_DATABASE_URL`, then restart
both processes. `/health/ready` also rejects an incomplete schema.

In Admin, 100% upload means file bytes have been sent, not that ingestion has
completed. The UI shows a separate waiting-for-API state and limits batch request
waiting to 60 seconds. After a timeout, inspect the document list before retrying:
the server might already have saved some files, and retrying creates new versions.
