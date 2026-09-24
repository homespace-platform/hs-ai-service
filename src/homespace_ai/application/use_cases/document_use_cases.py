from dataclasses import dataclass
from typing import Any
import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.core.config import Settings
from homespace_ai.knowledge.ingestion.parser import DocumentParser, MarkdownDocumentParser
from homespace_ai.models.knowledge import KnowledgeDocument, KnowledgeDocumentVersion, KnowledgeIngestionJob
from homespace_ai.repositories.document_repo import DocumentRepository
from homespace_ai.repositories.job_repo import JobRepository


@dataclass
class UploadResult:
    document_id: str
    version_id: str
    version: str
    status: str
    is_new_version: bool
    title: str
    visibility: str


class DocumentUseCases:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        parser: DocumentParser | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.doc_repo = DocumentRepository(session)
        self.job_repo = JobRepository(session)
        self.parser = parser or MarkdownDocumentParser()

    async def upload_document(
        self,
        filename: str,
        content: bytes,
        created_by: str | None = None,
    ) -> UploadResult:
        # Parse file securely
        parsed = self.parser.parse(
            filename=filename,
            content=content,
            max_size_bytes=self.settings.upload_max_size_bytes,
        )

        # Check if document already exists
        existing_doc = await self.doc_repo.get_by_document_id(parsed.document_id)

        if not existing_doc:
            # Create new document in DRAFT status
            doc = await self.doc_repo.create_document(
                document_id=parsed.document_id,
                title=parsed.title,
                audience=parsed.audience,
                visibility=parsed.visibility,
                locale=parsed.locale,
                category=parsed.category,
                created_by=created_by,
                status="DRAFT",
            )
            # Create new version in DRAFT status
            ver = await self.doc_repo.create_version(
                document_id=parsed.document_id,
                version=parsed.version,
                raw_markdown=parsed.raw_markdown,
                content_hash=parsed.content_hash,
                metadata_snapshot=parsed.metadata,
                embedding_model_id=self.settings.embedding_identity,
                chunk_config_version=self.settings.chunk_config_version,
                status="DRAFT",
            )
            await self.session.commit()
            return UploadResult(
                document_id=doc.document_id,
                version_id=str(ver.id),
                version=ver.version,
                status=ver.status,
                is_new_version=True,
                title=doc.title,
                visibility=doc.visibility,
            )

        # If document exists, check for duplicate content/model/chunk configuration
        existing_ver = await self.doc_repo.get_version_by_hash(
            document_id=parsed.document_id,
            content_hash=parsed.content_hash,
            embedding_model_id=self.settings.embedding_identity,
            chunk_config_version=self.settings.chunk_config_version,
        )
        if existing_ver:
            # Re-upload with same checksum and config: return existing version without re-creating
            return UploadResult(
                document_id=existing_doc.document_id,
                version_id=str(existing_ver.id),
                version=existing_ver.version,
                status=existing_ver.status,
                is_new_version=False,
                title=existing_doc.title,
                visibility=existing_doc.visibility,
            )

        # Content changed: create a new DRAFT version
        ver = await self.doc_repo.create_version(
            document_id=parsed.document_id,
            version=parsed.version,
            raw_markdown=parsed.raw_markdown,
            content_hash=parsed.content_hash,
            metadata_snapshot=parsed.metadata,
            embedding_model_id=self.settings.embedding_identity,
            chunk_config_version=self.settings.chunk_config_version,
            status="DRAFT",
        )
        await self.session.commit()

        return UploadResult(
            document_id=existing_doc.document_id,
            version_id=str(ver.id),
            version=ver.version,
            status=ver.status,
            is_new_version=True,
            title=existing_doc.title,
            visibility=existing_doc.visibility,
        )

    async def patch_document(
        self,
        document_id: str,
        title: str | None = None,
        category: str | None = None,
        visibility: str | None = None,
        audience: list[str] | None = None,
    ) -> KnowledgeDocument:
        doc = await self.doc_repo.get_by_document_id(document_id)
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document '{document_id}' not found.",
            )

        if doc.status == "ARCHIVED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot modify an ARCHIVED document.",
            )

        if doc.active_version_id or not doc.versions or doc.versions[0].status != "DRAFT":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only metadata of a new, unpublished DRAFT document can be changed.",
            )

        if visibility is not None and visibility not in ("public", "admin"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid visibility '{visibility}', must be 'public' or 'admin'.",
            )

        updated_doc = await self.doc_repo.update_metadata(
            document=doc,
            title=title,
            category=category,
            visibility=visibility,
            audience=audience,
        )
        await self.session.commit()
        return updated_doc

    async def approve_document(self, document_id: str) -> dict[str, Any]:
        doc = await self.doc_repo.get_by_document_id(document_id)
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document '{document_id}' not found.",
            )

        if doc.status == "ARCHIVED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot approve an ARCHIVED document.",
            )

        # Find latest version to approve
        if not doc.versions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Document '{document_id}' has no versions.",
            )

        latest_version = doc.versions[0]
        if latest_version.status != "DRAFT":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Only a DRAFT version can be approved (current: {latest_version.status}).",
            )
        if latest_version.embedding_model_id != self.settings.embedding_identity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Embedding model revision changed; re-upload the Markdown file before approving.",
            )

        await self.doc_repo.approve_document(document=doc, version=latest_version)
        await self.session.commit()

        return {
            "document_id": doc.document_id,
            "version_id": str(latest_version.id),
            "version": latest_version.version,
            "status": "APPROVED",
        }

    async def publish_document(self, document_id: str) -> dict[str, Any]:
        doc = await self.doc_repo.get_by_document_id(document_id)
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document '{document_id}' not found.",
            )

        if not doc.versions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Document '{document_id}' has no versions.",
            )

        latest_version = doc.versions[0]
        if latest_version.status not in ("APPROVED", "FAILED"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Only APPROVED or FAILED versions can be published. Current status is '{latest_version.status}'.",
            )
        if latest_version.embedding_model_id != self.settings.embedding_identity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Embedding model revision changed; re-upload the Markdown file before publishing.",
            )

        # Check if an active job already exists
        existing_job = await self.job_repo.get_active_job_for_version(latest_version.id)
        if existing_job:
            return {
                "message": "Ingestion job is already running or queued.",
                "job_id": str(existing_job.id),
                "status": existing_job.status,
                "document_id": document_id,
                "version_id": str(latest_version.id),
            }

        job = await self.job_repo.create_job(
            version_id=latest_version.id,
            document_id=document_id,
            job_type="INGEST",
            max_attempts=self.settings.worker_max_attempts,
        )
        await self.session.commit()

        return {
            "message": "Ingestion job enqueued successfully.",
            "job_id": str(job.id),
            "status": job.status,
            "document_id": document_id,
            "version_id": str(latest_version.id),
        }

    async def reindex_document(self, document_id: str) -> dict[str, Any]:
        doc = await self.doc_repo.get_by_document_id(document_id)
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document '{document_id}' not found.",
            )

        if doc.status != "APPROVED" or not doc.active_version_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only an active, published document can be reindexed.",
            )

        target_version_id = doc.active_version_id
        active_version = await self.doc_repo.get_version_by_id(target_version_id)
        if not active_version or active_version.embedding_model_id != self.settings.embedding_identity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Embedding model changed; upload a new document version before publishing.",
            )

        # Check if active job exists
        existing_job = await self.job_repo.get_active_job_for_version(target_version_id)
        if existing_job:
            return {
                "message": "Reindex job is already running or queued.",
                "job_id": str(existing_job.id),
                "status": existing_job.status,
                "document_id": document_id,
                "version_id": str(target_version_id),
            }

        job = await self.job_repo.create_job(
            version_id=target_version_id,
            document_id=document_id,
            job_type="REINDEX",
            max_attempts=self.settings.worker_max_attempts,
        )
        await self.session.commit()

        return {
            "message": "Reindex job enqueued successfully.",
            "job_id": str(job.id),
            "status": job.status,
            "document_id": document_id,
            "version_id": str(target_version_id),
        }

    async def archive_document(self, document_id: str) -> dict[str, Any]:
        doc = await self.doc_repo.get_by_document_id(document_id)
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document '{document_id}' not found.",
            )

        await self.doc_repo.archive_document(document_id)
        await self.session.commit()

        return {
            "message": f"Document '{document_id}' has been archived and removed from retrieval.",
            "document_id": document_id,
            "status": "ARCHIVED",
        }
