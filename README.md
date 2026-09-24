# HomeSpace AI Service

FastAPI microservice for HomeSpace AI capabilities. The service follows the current platform integration pattern:

- Listens on port `8084` in local development.
- Registers as `HS-AI-SERVICE` with Eureka at `http://localhost:8761/eureka`.
- Is reached by client applications through API Gateway at `/api/v1/ai/**`.
- Receives trusted `X-User-*` identity and authority headers after Gateway validates the Keycloak JWT.
- Calls other HomeSpace APIs through Gateway and forwards the incoming Bearer token.

## Start locally

From this directory in PowerShell:

```powershell
Copy-Item .env.example .env
uv sync --frozen
uv run uvicorn homespace_ai.main:app --app-dir src --host 0.0.0.0 --port 8084 --reload
```

The local `.env` is ignored by Git. Set `EUREKA_INSTANCE_HOSTNAME` to a routable host IP if automatic address detection selects the wrong adapter. The instance must be reachable by the Gateway machine.

Available endpoints:

| Purpose | Direct service URL | Through Gateway |
|---|---|---|
| Liveness | `http://localhost:8084/health/live` | `/api/v1/ai/health/live` |
| Readiness | `http://localhost:8084/health/ready` | `/api/v1/ai/health/ready` |
| Eureka/Gateway ping | `http://localhost:8084/ping` | `GET /api/v1/ai/ping` |
| Swagger UI | `http://localhost:8084/docs` | Requires JWT through Gateway |
| Current Gateway identity | `http://localhost:8084/auth/me` | `GET /api/v1/ai/auth/me` |

`/auth/me` requires a valid JWT at Gateway. It returns the identity headers injected by Gateway in the shared `{ "code": 1000, "result": ... }` envelope used by Chat and News. `/ping` returns `"pong"` like those services.

## API conventions

Gateway strips `/api/v1/ai` before forwarding a request. Therefore, FastAPI endpoints are declared at their service-local paths (`/ping`, `/auth/me`, `/knowledge/...`). Public API callers use `/api/v1/ai/ping`, `/api/v1/ai/auth/me`, and `/api/v1/ai/knowledge/...`.

All user-specific endpoints must derive identity from `get_current_user`; never accept a user ID or authority from the request body. Keep this service port private to the development network. Client traffic should enter through Gateway so users cannot forge `X-User-*` headers.

## Calling other services

Inject `GatewayApiClient` from `app.state.gateway_client` in a route or application service and call a non-AI `/api/v1/...` path. The client forwards only the caller's `Authorization: Bearer ...` header. Gateway validates that token and creates fresh `X-User-*` headers on the downstream request. Calls to `/api/v1/ai/**` are rejected to prevent loops.

## Dependency workflow

Add runtime packages with `uv add <package>` and development packages with `uv add --dev <package>`. Commit `pyproject.toml` and `uv.lock`; do not commit `.venv` or `.env`. Recreate the pinned environment with `uv sync --frozen`.
