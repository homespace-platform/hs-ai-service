from functools import lru_cache
from typing import ClassVar, Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DEFAULT_E5_REVISION: ClassVar[str] = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
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

    # Internal secret shared between Gateway and AI service to prevent forged headers
    gateway_internal_secret: str = Field(
        default="",
        alias="GATEWAY_INTERNAL_SECRET",
    )

    # Database configuration (PostgreSQL with pgvector)
    ai_database_url: str = Field(
        default="postgresql+asyncpg://homespace:homespace123@localhost:5433/homespace_ai",
        alias="AI_DATABASE_URL",
    )
    ai_mongodb_uri: str = Field(
        default="mongodb://homespace:homespace123@localhost:27017/homespace_ai?authSource=admin",
        alias="AI_MONGODB_URI",
    )
    # Local embedding configuration
    embedding_model_id: str = Field(
        default="intfloat/multilingual-e5-small",
        alias="EMBEDDING_MODEL_ID",
    )
    embedding_model_revision: str = Field(
        default=DEFAULT_E5_REVISION,
        alias="EMBEDDING_MODEL_REVISION",
    )

    @field_validator("embedding_model_revision", mode="before")
    @classmethod
    def default_empty_revision(cls, value: str | None) -> str:
        return value or cls.DEFAULT_E5_REVISION

    @property
    def embedding_identity(self) -> str:
        return f"{self.embedding_model_id}@{self.embedding_model_revision}"
    model_cache_dir: str = Field(
        default="./models_cache",
        alias="MODEL_CACHE_DIR",
    )

    # Chunking configuration
    chunk_target_tokens: int = Field(default=350, ge=100, le=500, alias="CHUNK_TARGET_TOKENS")
    chunk_overlap_tokens: int = Field(default=50, ge=0, le=100, alias="CHUNK_OVERLAP_TOKENS")
    chunk_max_tokens: int = Field(default=450, ge=200, le=510, alias="CHUNK_MAX_TOKENS")
    chunk_config_version: str = Field(default="v1", alias="CHUNK_CONFIG_VERSION")
    upload_max_size_bytes: int = Field(default=1048576, ge=1024, alias="UPLOAD_MAX_SIZE_BYTES")

    # Retrieval configuration
    retrieval_top_k: int = Field(default=5, ge=1, le=20, alias="RETRIEVAL_TOP_K")
    retrieval_min_similarity: float = Field(default=0.80, ge=0.0, le=1.0, alias="RETRIEVAL_MIN_SIMILARITY")

    # OpenAI Responses API. Embeddings remain local and independent of generation.
    generation_provider: Literal["disabled", "openai"] = Field(default="disabled", alias="GENERATION_PROVIDER")
    openai_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENAI_API_KEY", repr=False)
    openai_rag_model: str = Field(default="gpt-4o-mini", min_length=1, alias="OPENAI_RAG_MODEL")
    openai_tool_model: str = Field(default="gpt-4.1-mini", min_length=1, alias="OPENAI_TOOL_MODEL")
    openai_listing_model: str = Field(default="gpt-4.1-mini", min_length=1, alias="OPENAI_LISTING_MODEL")
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = Field(
        default="low", alias="OPENAI_REASONING_EFFORT")
    openai_rag_max_output_tokens: int = Field(default=4096, ge=64, le=32768, alias="OPENAI_RAG_MAX_OUTPUT_TOKENS")
    openai_tool_max_output_tokens: int = Field(default=4096, ge=64, le=32768, alias="OPENAI_TOOL_MAX_OUTPUT_TOKENS")
    openai_listing_max_output_tokens: int = Field(default=16384, ge=64, le=32768, alias="OPENAI_LISTING_MAX_OUTPUT_TOKENS")
    generation_temperature: float = Field(default=0.2, ge=0.0, le=1.0, alias="GENERATION_TEMPERATURE")
    generation_timeout_seconds: float = Field(default=90.0, ge=1.0, le=180.0, alias="GENERATION_TIMEOUT_SECONDS")

    # Worker configuration
    worker_poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=30.0, alias="WORKER_POLL_INTERVAL_SECONDS")
    worker_lease_duration_seconds: int = Field(default=60, ge=10, le=600, alias="WORKER_LEASE_DURATION_SECONDS")
    worker_max_attempts: int = Field(default=3, ge=1, le=10, alias="WORKER_MAX_ATTEMPTS")


@lru_cache
def get_settings() -> Settings:
    return Settings()
