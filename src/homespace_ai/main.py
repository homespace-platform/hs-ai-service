import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from homespace_ai.api.v1.router import router as api_router
from homespace_ai.clients.gateway import GatewayApiClient
from homespace_ai.core.config import get_settings
from homespace_ai.core.database import engine
from homespace_ai.core.logging import configure_logging
from homespace_ai.discovery.eureka import EurekaRegistration

settings = get_settings()
configure_logging(settings.log_level)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.eureka_registered = False
    async with httpx.AsyncClient(timeout=5.0) as eureka_http:
        registration = EurekaRegistration(settings, eureka_http)
        application.state.eureka_registered = await registration.register()
        heartbeat_task = asyncio.create_task(registration.heartbeat_loop())
        application.state.gateway_client = GatewayApiClient(
            str(settings.gateway_base_url), settings.gateway_request_timeout_seconds
        )
        logger.info(
            "service_started",
            service=settings.app_name,
            environment=settings.app_env,
            port=settings.port,
            eureka_host=registration.host,
            eureka_registered=application.state.eureka_registered,
        )
        # Warm up embedding model in background so user queries respond in milliseconds
        from homespace_ai.api.v1.admin_knowledge import get_embedder
        warmup_task = asyncio.create_task(asyncio.to_thread(get_embedder, settings))
        try:
            yield
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            await application.state.gateway_client.aclose()
            await registration.deregister()
            logger.info("service_stopped", service=settings.app_name)


from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi import Request

app = FastAPI(
    title="HomeSpace AI Service",
    version="0.1.0",
    description="AI capabilities for HomeSpace, integrated with Gateway and Eureka.",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    raw_body = await request.body()
    logger.error(
        "request_validation_error",
        url=str(request.url),
        body=raw_body.decode("utf-8", errors="replace"),
        errors=exc.errors(),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


app.include_router(api_router)


@app.get("/health/live", tags=["Health"])
async def liveness() -> dict[str, str]:
    return {"status": "UP"}


@app.get("/health/ready", tags=["Health"])
async def readiness() -> dict[str, str]:
    configured_secret = settings.gateway_internal_secret.strip()
    if settings.app_env != "local" and (not configured_secret or configured_secret == "hs-internal-gateway-secret-dev-2026" or len(configured_secret) < 32):
        raise HTTPException(status_code=503, detail="Gateway trust is not securely configured.")
    try:
        async with asyncio.timeout(3):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="AI database is unavailable.") from None
    return {
        "status": "READY",
        "service": settings.app_name,
        "eureka": "UP" if app.state.eureka_registered else "DEGRADED",
    }
