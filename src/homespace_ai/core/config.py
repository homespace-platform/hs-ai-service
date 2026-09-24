from functools import lru_cache

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    app_name: str = Field(default="hs-ai-service", alias="APP_NAME")
    app_env: str = Field(default="local", alias="APP_ENV")
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8084, ge=1, le=65535, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    eureka_client_service_url: HttpUrl = Field(
        default=HttpUrl("http://localhost:8761/eureka"),
        alias="EUREKA_CLIENT_SERVICE_URL",
    )
    eureka_instance_hostname: str = Field(
        default="localhost", alias="EUREKA_INSTANCE_HOSTNAME"
    )
    eureka_heartbeat_interval_seconds: int = Field(
        default=30, ge=5, alias="EUREKA_HEARTBEAT_INTERVAL_SECONDS"
    )

    gateway_base_url: HttpUrl = Field(
        default=HttpUrl("http://localhost:8080"), alias="GATEWAY_BASE_URL"
    )
    gateway_request_timeout_seconds: float = Field(
        default=15, gt=0, le=120, alias="GATEWAY_REQUEST_TIMEOUT_SECONDS"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
