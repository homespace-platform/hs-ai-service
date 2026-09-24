from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from homespace_ai.models.knowledge import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_document_id(
        self, document_id: str, include_versions: bool = True
    ) -> KnowledgeDocument | None:
        stmt = select(KnowledgeDocument).where(KnowledgeDocument.document_id == document_id)
        if include_versions:
            stmt = stmt.options(selectinload(KnowledgeDocument.versions))
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_version_by_id(self, version_id: uuid.UUID) -> KnowledgeDocumentVersion | None:
        stmt = (
            select(KnowledgeDocumentVersion)
            .where(KnowledgeDocumentVersion.id == version_id)
            .options(
                selectinload(KnowledgeDocumentVersion.document),
                selectinload(KnowledgeDocumentVersion.jobs),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_version_by_hash(
        self, document_id: str, content_hash: str, embedding_model_id: str, chunk_config_version: str
    ) -> KnowledgeDocumentVersion | None:
        stmt = (
            select(KnowledgeDocumentVersion)
            .where(
                KnowledgeDocumentVersion.document_id == document_id,
                KnowledgeDocumentVersion.content_hash == content_hash,
                KnowledgeDocumentVersion.embedding_model_id == embedding_model_id,
                KnowledgeDocumentVersion.chunk_config_version == chunk_config_version,
            )
            .order_by(KnowledgeDocumentVersion.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def list_documents(
        self,
        offset: int = 0,
        limit: int = 20,
        status: str | None = None,
        visibility: str | None = None,
    ) -> tuple[list[KnowledgeDocument], int]:
        query = select(KnowledgeDocument)
        count_query = select(func.count()).select_from(KnowledgeDocument)

        if status:
            query = query.where(KnowledgeDocument.status == status)
            count_query = count_query.where(KnowledgeDocument.status == status)
        if visibility:
            query = query.where(KnowledgeDocument.visibility == visibility)
            count_query = count_query.where(KnowledgeDocument.visibility == visibility)

        query = (
            query.order_by(KnowledgeDocument.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .options(selectinload(KnowledgeDocument.versions))
        )

        total = (await self.session.execute(count_query)).scalar_one()
        items = (await self.session.execute(query)).scalars().all()
        return list(items), total

    async def create_document(
        self,
        document_id: str,
        title: str,
        audience: list[str],
        visibility: str,
        locale: str,
        created_by: str | None,
        category: str | None = None,
        status: str = "DRAFT",
    ) -> KnowledgeDocument:
        doc = KnowledgeDocument(
            document_id=document_id,
            title=title,
            audience=audience,
            visibility=visibility,
            locale=locale,
            category=category,
            created_by=created_by,
            status=status,
        )
        self.session.add(doc)
        await self.session.flush()
        return doc

    async def create_version(
        self,
        document_id: str,
        version: str,
        raw_markdown: str,
        content_hash: str,
        metadata_snapshot: dict[str, Any],
        embedding_model_id: str,
        chunk_config_version: str,
        status: str = "DRAFT",
    ) -> KnowledgeDocumentVersion:
        ver = KnowledgeDocumentVersion(
            document_id=document_id,
            version=version,
            raw_markdown=raw_markdown,
            content_hash=content_hash,
            metadata_snapshot=metadata_snapshot,
            embedding_model_id=embedding_model_id,
            chunk_config_version=chunk_config_version,
            status=status,
        )
        self.session.add(ver)
        await self.session.flush()
        return ver

    async def update_metadata(
        self,
        document: KnowledgeDocument,
        title: str | None = None,
        category: str | None = None,
        visibility: str | None = None,
        audience: list[str] | None = None,
    ) -> KnowledgeDocument:
        if title is not None:
            document.title = title
        if category is not None:
            document.category = category
        if visibility is not None:
            document.visibility = visibility
        if audience is not None:
            document.audience = audience
        if document.versions and document.versions[0].status == "DRAFT":
            version = document.versions[0]
            version.metadata_snapshot = {
                **version.metadata_snapshot,
                "title": document.title,
                "category": document.category,
                "visibility": document.visibility,
                "audience": document.audience,
            }
        document.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return document

    async def approve_document(
        self, document: KnowledgeDocument, version: KnowledgeDocumentVersion
    ) -> None:
        document.status = "APPROVED"
        document.updated_at = datetime.now(timezone.utc)
        version.status = "APPROVED"
        version.updated_at = datetime.now(timezone.utc)
        await self.session.flush()

    async def activate_version(
        self, document_id: str, version_id: uuid.UUID
    ) -> None:
        """Atomically activates the newly published version."""
        now = datetime.now(timezone.utc)
        version = await self.get_version_by_id(version_id)
        if not version or version.document_id != document_id:
            raise ValueError("Version does not belong to this document.")
        metadata = version.metadata_snapshot

        # Switch both ACL metadata and active version in one transaction.
        result = await self.session.execute(
            update(KnowledgeDocument)
            .where(
                KnowledgeDocument.document_id == document_id,
                KnowledgeDocument.status != "ARCHIVED",
            )
            .values(
                active_version_id=version_id,
                title=metadata["title"],
                category=metadata.get("category"),
                visibility=metadata["visibility"],
                audience=metadata["audience"],
                locale=metadata["locale"],
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise ValueError("Document is archived or unavailable for activation.")
        # Mark version as PUBLISHED
        await self.session.execute(
            update(KnowledgeDocumentVersion)
            .where(KnowledgeDocumentVersion.id == version_id)
            .values(status="PUBLISHED", updated_at=now)
        )
        await self.session.flush()

    async def archive_document(self, document_id: str) -> None:
        now = datetime.now(timezone.utc)
        # Setting status to ARCHIVED immediately removes document from retrieval
        await self.session.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.document_id == document_id)
            .values(status="ARCHIVED", updated_at=now)
        )
        await self.session.execute(
            update(KnowledgeDocumentVersion)
            .where(KnowledgeDocumentVersion.document_id == document_id)
            .values(status="ARCHIVED", updated_at=now)
        )
        await self.session.flush()
