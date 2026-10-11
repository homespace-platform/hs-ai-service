from dataclasses import dataclass
from typing import Any
import hashlib
import uuid
from datetime import datetime, timezone

import yaml

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from homespace_ai.core.config import Settings
from homespace_ai.knowledge.ingestion.parser import DocumentParser, MarkdownDocumentParser
from homespace_ai.knowledge.filenames import normalize_filename
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
    job_id: str
    source_filename: str


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
        expected_version_id: str | None = None,
        target_document_id: str | None = None,
    ) -> UploadResult:
        filename_key = normalize_filename(filename)
        if filename_key == "readme.md":
            raise HTTPException(400, "README.md is a folder guide, not a knowledge document.")
        parsed = self.parser.parse(
            filename=filename,
            content=content,
            max_size_bytes=self.settings.upload_max_size_bytes,
        )

        # Serialize same-name uploads, including the first insert. Values are bound, never SQL identifiers.
        await self.session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:filename, 0))"),
                                   {"filename": "filename:" + filename_key})
        doc = await self.doc_repo.get_by_filename(filename_key)
        if target_document_id and (not doc or doc.document_id != target_document_id):
            raise HTTPException(409, "Document filename changed; reload the editor before updating.")
        if doc and doc.status == "ARCHIVED":
            raise HTTPException(409, "This document is archived. It cannot be replaced until restored.")
        if doc and expected_version_id and (not doc.versions or str(doc.versions[0].id) != expected_version_id):
            raise HTTPException(409, "A newer version exists. Reload the document before saving your changes.")
        if not doc:
            # Preserve a free human-readable ID for existing citations/benchmarks, but never merge by that ID.
            await self.session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
                                       {"identity": "document-id:" + parsed.document_id})
            used_id = await self.doc_repo.get_by_document_id(parsed.document_id, include_versions=False)
            document_id = ("kb-" + hashlib.sha256(filename_key.encode("utf-8")).hexdigest()[:40]
                           if used_id else parsed.document_id)
            doc = await self.doc_repo.create_document(
                document_id=document_id, title=parsed.title, audience=parsed.audience,
                visibility=parsed.visibility, locale=parsed.locale, category=parsed.category,
                created_by=created_by, status="APPROVED", source_filename=filename_key,
            )
            version_number = 1
        else:
            version_number = max((version.version_number for version in doc.versions), default=0) + 1
            doc.source_filename = filename_key
            # Do not change serving ACL/title before the new version activates.
            doc.status = "APPROVED"
            doc.updated_at = datetime.now(timezone.utc)
        metadata = {**parsed.metadata, "document_id": doc.document_id, "version": str(version_number)}
        ver = await self.doc_repo.create_version(
            document_id=doc.document_id,
            version=str(version_number),
            version_number=version_number,
            raw_markdown=parsed.raw_markdown,
            content_hash=parsed.content_hash,
            metadata_snapshot=metadata,
            embedding_model_id=self.settings.embedding_identity,
            chunk_config_version=self.settings.chunk_config_version,
            status="APPROVED",
        )
        job = await self.job_repo.create_job(
            version_id=ver.id, document_id=doc.document_id, job_type="INGEST",
            max_attempts=self.settings.worker_max_attempts,
        )
        await self.session.commit()
        return UploadResult(
            document_id=doc.document_id,
            version_id=str(ver.id),
            version=ver.version,
            status=job.status,
            is_new_version=True,
            title=parsed.title,
            visibility=parsed.visibility,
            job_id=str(job.id),
            source_filename=filename_key,
        )

    async def get_content(self, document_id: str) -> dict[str, Any]:
        doc = await self.doc_repo.get_by_document_id(document_id)
        if not doc or not doc.versions:
            raise HTTPException(404, "Document not found.")
        version = doc.versions[0]
        metadata = {key: value for key, value in version.metadata_snapshot.items()
                    if key != "original_filename"}
        metadata.update(document_id=doc.document_id, version=version.version)
        filename = doc.source_filename or version.metadata_snapshot.get("original_filename") or f"{doc.document_id}.md"
        markdown = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n\n" + version.raw_markdown + "\n"
        return {"documentId": doc.document_id, "sourceFilename": filename,
                "versionId": str(version.id), "version": version.version, "markdown": markdown}

    async def update_content(self, document_id: str, markdown: str, expected_version_id: str,
                             created_by: str | None = None) -> UploadResult:
        current = await self.get_content(document_id)
        return await self.upload_document(
            filename=current["sourceFilename"], content=markdown.encode("utf-8"), created_by=created_by,
            expected_version_id=expected_version_id, target_document_id=document_id,
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
