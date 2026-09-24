import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homespace_ai.core.database import Base


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    locale: Mapped[str] = mapped_column(String(20), default="vi-VN", nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(20), default="public", nullable=False, index=True
    )  # 'public' or 'admin'
    audience: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), default="DRAFT", nullable=False, index=True
    )  # 'DRAFT', 'APPROVED', 'ARCHIVED'
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    versions: Mapped[list["KnowledgeDocumentVersion"]] = relationship(
        "KnowledgeDocumentVersion",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="desc(KnowledgeDocumentVersion.created_at)",
    )


class KnowledgeDocumentVersion(Base):
    __tablename__ = "knowledge_document_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(30), nullable=False)
    raw_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    metadata_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    embedding_model_id: Mapped[str] = mapped_column(String(100), nullable=False)
    chunk_config_version: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default="DRAFT", nullable=False, index=True
    )  # 'DRAFT', 'APPROVED', 'PUBLISHING', 'PUBLISHED', 'FAILED', 'ARCHIVED'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    document: Mapped["KnowledgeDocument"] = relationship(
        "KnowledgeDocument", back_populates="versions"
    )
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        "KnowledgeChunk",
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="KnowledgeChunk.chunk_index",
    )
    jobs: Mapped[list["KnowledgeIngestionJob"]] = relationship(
        "KnowledgeIngestionJob",
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="desc(KnowledgeIngestionJob.created_at)",
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_document_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    document_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding = mapped_column(Vector(384), nullable=False)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    version: Mapped["KnowledgeDocumentVersion"] = relationship(
        "KnowledgeDocumentVersion", back_populates="chunks"
    )


class KnowledgeIngestionJob(Base):
    __tablename__ = "knowledge_ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_document_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    document_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    job_type: Mapped[str] = mapped_column(
        String(30), default="INGEST", nullable=False
    )  # 'INGEST', 'REINDEX'
    status: Mapped[str] = mapped_column(
        String(30), default="QUEUED", index=True, nullable=False
    )  # 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED'
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    version: Mapped["KnowledgeDocumentVersion"] = relationship(
        "KnowledgeDocumentVersion", back_populates="jobs"
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(100), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ONLINE", nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

