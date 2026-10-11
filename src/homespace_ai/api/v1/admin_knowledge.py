import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from homespace_ai.api.v1.schemas import (
    DiagnosticChunkItem,
    DiagnosticSearchRequest,
    DiagnosticSearchResponse,
    DocumentDetailResponse,
    DocumentSummaryResponse,
    JobSummaryResponse,
    PatchDocumentRequest,
    VersionSummaryResponse,
    UpdateDocumentContentRequest,
)
from homespace_ai.application.use_cases.document_use_cases import DocumentUseCases
from homespace_ai.core.api_response import ApiResponse
from homespace_ai.core.config import Settings, get_settings
from homespace_ai.core.database import get_db
from homespace_ai.core.security import UserContext, require_admin
from homespace_ai.knowledge.ingestion.embedder import LocalE5EmbeddingAdapter
from homespace_ai.repositories.chunk_repo import ChunkRepository
from homespace_ai.repositories.document_repo import DocumentRepository
from homespace_ai.repositories.job_repo import JobRepository

router = APIRouter(prefix="/admin/knowledge", tags=["Admin Knowledge"])

# Shared local embedder singleton
_embedder_instance = None


def get_embedder(settings: Settings = Depends(get_settings)):
    global _embedder_instance
    if _embedder_instance is None:
        _embedder_instance = LocalE5EmbeddingAdapter(
            model_id=settings.embedding_model_id,
            revision=settings.embedding_model_revision,
            cache_dir=settings.model_cache_dir,
        )
    return _embedder_instance


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[dict[str, Any]],
)
async def upload_document(
    file: UploadFile = File(...),
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ApiResponse[dict[str, Any]]:
    """Admin upload always creates the next version and queues local ingestion."""
    content = await file.read(settings.upload_max_size_bytes + 1)
    if len(content) > settings.upload_max_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Markdown file exceeds the configured upload limit.",
        )
    filename = file.filename or "document.md"

    use_cases = DocumentUseCases(session=session, settings=settings)
    result = await use_cases.upload_document(
        filename=filename,
        content=content,
        created_by=admin.user_id,
    )

    return ApiResponse(
        code=1000,
        message="New version queued; the active version remains available until ingestion succeeds.",
        result=_upload_result(result),
    )


def _upload_result(result) -> dict[str, Any]:
    return {"documentId": result.document_id, "versionId": result.version_id, "version": result.version,
            "status": result.status, "isNewVersion": result.is_new_version, "title": result.title,
            "visibility": result.visibility, "jobId": result.job_id, "sourceFilename": result.source_filename}


@router.post("/documents/batch", status_code=202, response_model=ApiResponse[dict[str, Any]])
async def upload_documents_batch(
    files: list[UploadFile] = File(...),
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ApiResponse[dict[str, Any]]:
    if not 1 <= len(files) <= 50:
        raise HTTPException(400, "Select between 1 and 50 files per batch.")
    items = []
    for file in files:
        filename = file.filename or "document.md"
        try:
            content = await file.read(settings.upload_max_size_bytes + 1)
            result = await DocumentUseCases(session, settings).upload_document(filename, content, admin.user_id)
            items.append({"filename": filename, "success": True, "result": _upload_result(result)})
        except HTTPException as error:
            await session.rollback()
            items.append({"filename": filename, "success": False, "statusCode": error.status_code, "error": str(error.detail)})
        except Exception:
            await session.rollback()
            items.append({"filename": filename, "success": False, "statusCode": 500, "error": "Unable to save this file; other files continue."})
        finally:
            await file.close()
    return ApiResponse(code=1000, result={"items": items})


@router.get("/documents/{document_id}/content", response_model=ApiResponse[dict[str, Any]])
async def get_document_content(document_id: str, admin: UserContext = Depends(require_admin),
                               session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    return ApiResponse(code=1000, result=await DocumentUseCases(session, settings).get_content(document_id))


@router.put("/documents/{document_id}/content", status_code=202, response_model=ApiResponse[dict[str, Any]])
async def update_document_content(document_id: str, body: UpdateDocumentContentRequest,
                                  admin: UserContext = Depends(require_admin), session: AsyncSession = Depends(get_db),
                                  settings: Settings = Depends(get_settings)):
    result = await DocumentUseCases(session, settings).update_content(
        document_id, body.markdown, body.expectedVersionId, admin.user_id)
    return ApiResponse(code=1000, result=_upload_result(result))


@router.get(
    "/documents",
    response_model=ApiResponse[dict[str, Any]],
)
async def list_documents(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    visibility: str | None = Query(None),
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[dict[str, Any]]:
    """Lists knowledge documents with pagination and filters. Does not return raw markdown."""
    repo = DocumentRepository(session)
    docs, total = await repo.list_documents(
        offset=offset,
        limit=limit,
        status=status_filter,
        visibility=visibility,
    )

    items = [
        DocumentSummaryResponse(
            documentId=d.document_id,
            title=d.title,
            category=d.category,
            locale=d.locale,
            visibility=d.visibility,
            audience=d.audience,
            status=d.status,
            activeVersionId=str(d.active_version_id) if d.active_version_id else None,
            createdAt=d.created_at,
            updatedAt=d.updated_at,
            sourceFilename=d.source_filename,
            latestVersion=d.versions[0].version if d.versions else None,
            latestVersionStatus=d.versions[0].status if d.versions else None,
        )
        for d in docs
    ]

    return ApiResponse(
        code=1000,
        result={
            "items": [item.model_dump() for item in items],
            "total": total,
            "offset": offset,
            "limit": limit,
        },
    )


@router.get(
    "/documents/{document_id}",
    response_model=ApiResponse[DocumentDetailResponse],
)
async def get_document_detail(
    document_id: str,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[DocumentDetailResponse]:
    """Retrieves document metadata, list of versions, active version, and latest job status."""
    doc_repo = DocumentRepository(session)
    job_repo = JobRepository(session)

    doc = await doc_repo.get_by_document_id(document_id, include_versions=True)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found.",
        )

    version_items = [
        VersionSummaryResponse(
            versionId=str(v.id),
            version=v.version,
            status=v.status,
            contentHash=v.content_hash,
            createdAt=v.created_at,
            metadata=v.metadata_snapshot,
            rawMarkdown=v.raw_markdown,
        )
        for v in doc.versions
    ]

    active_version = None
    if doc.active_version_id:
        for v in version_items:
            if v.versionId == str(doc.active_version_id):
                active_version = v
                break

    latest_job_summary = None
    if doc.versions:
        latest_v = doc.versions[0]
        job = await job_repo.get_latest_job_for_version(latest_v.id)
        if job:
            latest_job_summary = JobSummaryResponse(
                jobId=str(job.id),
                versionId=str(job.version_id),
                documentId=job.document_id,
                jobType=job.job_type,
                status=job.status,
                attempts=job.attempts,
                maxAttempts=job.max_attempts,
                errorMessage=job.error_message,
                createdAt=job.created_at,
                updatedAt=job.updated_at,
            )

    detail = DocumentDetailResponse(
        documentId=doc.document_id,
        title=doc.title,
        category=doc.category,
        locale=doc.locale,
        visibility=doc.visibility,
        audience=doc.audience,
        status=doc.status,
        activeVersionId=str(doc.active_version_id) if doc.active_version_id else None,
        createdAt=doc.created_at,
        updatedAt=doc.updated_at,
        sourceFilename=doc.source_filename,
        latestVersion=doc.versions[0].version if doc.versions else None,
        latestVersionStatus=doc.versions[0].status if doc.versions else None,
        versions=version_items,
        activeVersion=active_version,
        latestJob=latest_job_summary,
    )

    return ApiResponse(code=1000, result=detail)


@router.patch(
    "/documents/{document_id}",
    response_model=ApiResponse[DocumentSummaryResponse],
)
async def patch_document_metadata(
    document_id: str,
    body: PatchDocumentRequest,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ApiResponse[DocumentSummaryResponse]:
    """Modifies metadata of a draft document. Cannot patch archived documents."""
    use_cases = DocumentUseCases(session=session, settings=settings)
    doc = await use_cases.patch_document(
        document_id=document_id,
        title=body.title,
        category=body.category,
        visibility=body.visibility,
        audience=body.audience,
    )
    return ApiResponse(
        code=1000,
        result=DocumentSummaryResponse(
            documentId=doc.document_id,
            title=doc.title,
            category=doc.category,
            locale=doc.locale,
            visibility=doc.visibility,
            audience=doc.audience,
            status=doc.status,
            activeVersionId=str(doc.active_version_id) if doc.active_version_id else None,
            createdAt=doc.created_at,
            updatedAt=doc.updated_at,
        ),
    )


@router.post(
    "/documents/{document_id}/approve",
    response_model=ApiResponse[dict[str, Any]],
)
async def approve_document(
    document_id: str,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ApiResponse[dict[str, Any]]:
    """Approves a draft document version."""
    use_cases = DocumentUseCases(session=session, settings=settings)
    result = await use_cases.approve_document(document_id)
    return ApiResponse(code=1000, message="Document approved.", result=result)


@router.post(
    "/documents/{document_id}/publish",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[dict[str, Any]],
)
async def publish_document(
    document_id: str,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ApiResponse[dict[str, Any]]:
    """Enqueues document ingestion job. Returns 202 Accepted with jobId immediately."""
    use_cases = DocumentUseCases(session=session, settings=settings)
    result = await use_cases.publish_document(document_id)
    return ApiResponse(code=1000, message="Publishing job enqueued.", result=result)


@router.post(
    "/documents/{document_id}/reindex",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[dict[str, Any]],
)
async def reindex_document(
    document_id: str,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ApiResponse[dict[str, Any]]:
    """Enqueues reindexing job for the active version. Idempotent if job is already queued/running."""
    use_cases = DocumentUseCases(session=session, settings=settings)
    result = await use_cases.reindex_document(document_id)
    return ApiResponse(code=1000, message="Reindex job enqueued.", result=result)


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[JobSummaryResponse],
)
async def get_job_status(
    job_id: uuid.UUID,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[JobSummaryResponse]:
    """Retrieves ingestion job status, error details, attempts and timestamps."""
    repo = JobRepository(session)
    job = await repo.get_by_id(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )

    return ApiResponse(
        code=1000,
        result=JobSummaryResponse(
            jobId=str(job.id),
            versionId=str(job.version_id),
            documentId=job.document_id,
            jobType=job.job_type,
            status=job.status,
            attempts=job.attempts,
            maxAttempts=job.max_attempts,
            errorMessage=job.error_message,
            createdAt=job.created_at,
            updatedAt=job.updated_at,
        ),
    )


@router.get(
    "/worker-health",
    response_model=ApiResponse[dict[str, Any]],
)
async def get_worker_health(
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[dict[str, Any]]:
    """Checks whether any background ingestion worker is actively running."""
    job_repo = JobRepository(session)
    active_workers = await job_repo.get_active_workers(timeout_seconds=45)
    is_healthy = len(active_workers) > 0
    return ApiResponse(
        code=1000,
        result={
            "status": "HEALTHY" if is_healthy else "OFFLINE",
            "activeWorkers": len(active_workers),
            "workers": [
                {
                    "workerId": w.worker_id,
                    "hostname": w.hostname,
                    "pid": w.pid,
                    "lastHeartbeat": w.last_heartbeat.isoformat(),
                    "startedAt": w.started_at.isoformat(),
                }
                for w in active_workers
            ],
            "message": (
                "Worker đang hoạt động bình thường."
                if is_healthy
                else "Tiến trình nhúng (Worker) chưa chạy. Vui lòng chạy lệnh: uv run python -m homespace_ai.knowledge.ingestion.worker"
            ),
        },
    )


@router.post(
    "/documents/{document_id}/archive",
    response_model=ApiResponse[dict[str, Any]],
)
async def archive_document(
    document_id: str,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ApiResponse[dict[str, Any]]:
    """Archives document. Immediately excludes it from retrieval; keeps history."""
    use_cases = DocumentUseCases(session=session, settings=settings)
    result = await use_cases.archive_document(document_id)
    return ApiResponse(code=1000, message="Document archived.", result=result)


@router.post(
    "/search",
    response_model=ApiResponse[DiagnosticSearchResponse],
)
async def diagnostic_search(
    body: DiagnosticSearchRequest,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    embedder=Depends(get_embedder),
) -> ApiResponse[DiagnosticSearchResponse]:
    """Admin diagnostic retrieval endpoint using hybrid search.
    Allows testing similarity and keyword search with public or admin scope; does not call LLM.
    """
    allowed_visibilities = ["public"]
    if body.scope == "admin":
        allowed_visibilities.append("admin")

    query_vec = embedder.embed_query(body.query)
    chunk_repo = ChunkRepository(session)

    results = await chunk_repo.search_chunks_hybrid(
        query_text=body.query,
        query_vector=query_vec,
        top_k=body.topK,
        allowed_visibilities=allowed_visibilities,
        min_vector_similarity=body.minSimilarity,
        candidate_pool_size=20,
    )

    items = [
        DiagnosticChunkItem(
            documentId=c.document_id,
            title=c.chunk_metadata.get("title", ""),
            headingPath=c.heading_path,
            content=c.content,
            similarity=sim,
            chunkIndex=c.chunk_index,
            visibility=c.chunk_metadata.get("visibility", "public"),
        )
        for c, sim in results
    ]

    return ApiResponse(
        code=1000,
        result=DiagnosticSearchResponse(
            query=body.query,
            scope=body.scope,
            count=len(items),
            results=items,
        ),
    )


@router.post(
    "/test-ask",
    response_model=ApiResponse[dict[str, Any]],
)
async def test_ask(
    body: DiagnosticSearchRequest,
    admin: UserContext = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    embedder=Depends(get_embedder),
) -> ApiResponse[dict[str, Any]]:
    """Allows admin to test a full query with answer synthesis, retrieved chunks, and actual citations."""
    from homespace_ai.application.use_cases.ask_use_cases import AskUseCases
    from homespace_ai.clients.generative import get_generative_client

    generative_client = get_generative_client(settings)
    use_cases = AskUseCases(
        session=session,
        settings=settings,
        embedder=embedder,
        generative_client=generative_client,
    )
    result = await use_cases.ask(
        question=body.query,
        user_role="ADMIN" if body.scope == "admin" else "USER",
    )
    return ApiResponse(
        code=1000,
        result={
            "answer": result.answer,
            "status": result.status,
            "citations": result.citations,
            "requestId": result.request_id,
        },
    )

