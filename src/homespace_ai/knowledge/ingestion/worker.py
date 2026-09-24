import asyncio
import os
import signal
import socket
import sys
import uuid
import structlog

from homespace_ai.core.config import get_settings
from homespace_ai.core.database import async_session_maker
from homespace_ai.knowledge.ingestion.chunker import MarkdownChunker
from homespace_ai.knowledge.ingestion.embedder import BaseEmbeddingAdapter, LocalE5EmbeddingAdapter
from homespace_ai.repositories.chunk_repo import ChunkRepository
from homespace_ai.repositories.document_repo import DocumentRepository
from homespace_ai.repositories.job_repo import JobRepository

logger = structlog.get_logger(__name__)


class IngestionWorker:
    def __init__(
        self,
        embedder: BaseEmbeddingAdapter | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.worker_id = worker_id or f"worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.embedder = embedder or LocalE5EmbeddingAdapter(
            model_id=self.settings.embedding_model_id,
            revision=self.settings.embedding_model_revision,
            cache_dir=self.settings.model_cache_dir,
        )
        self.running = False

    async def run(self) -> None:
        self.running = True
        hostname = socket.gethostname()
        pid = os.getpid()

        logger.info(
            "worker_started",
            worker_id=self.worker_id,
            poll_interval=self.settings.worker_poll_interval_seconds,
            lease_duration=self.settings.worker_lease_duration_seconds,
        )

        # Record initial heartbeat
        try:
            async with async_session_maker() as session:
                await JobRepository(session).record_worker_heartbeat(
                    worker_id=self.worker_id, hostname=hostname, pid=pid
                )
                await session.commit()
        except Exception as e:
            logger.warn("worker_initial_heartbeat_failed", error=str(e))

        loop = asyncio.get_running_loop()
        last_heartbeat_time = loop.time()

        while self.running:
            try:
                now = loop.time()
                if now - last_heartbeat_time >= 10.0:
                    async with async_session_maker() as session:
                        await JobRepository(session).record_worker_heartbeat(
                            worker_id=self.worker_id, hostname=hostname, pid=pid
                        )
                        await session.commit()
                    last_heartbeat_time = now

                processed = await self.process_next_job()
                if not processed:
                    await asyncio.sleep(self.settings.worker_poll_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("worker_loop_error", worker_id=self.worker_id, error_type=type(e).__name__)
                await asyncio.sleep(self.settings.worker_poll_interval_seconds)

        # Unregister worker on graceful exit
        try:
            async with async_session_maker() as session:
                await JobRepository(session).unregister_worker(self.worker_id)
                await session.commit()
        except Exception:
            pass

        logger.info("worker_stopped", worker_id=self.worker_id)

    def stop(self) -> None:
        self.running = False

    async def process_next_job(self) -> bool:
        """Claims and processes one job atomically. Returns True if a job was processed."""
        async with async_session_maker() as session:
            job_repo = JobRepository(session)
            document_repo = DocumentRepository(session)
            chunk_repo = ChunkRepository(session)

            job = await job_repo.claim_next_job(
                worker_id=self.worker_id,
                lease_seconds=self.settings.worker_lease_duration_seconds,
            )
            if not job:
                return False

            await session.commit()

        # Job claimed, now execute processing in a try/except
        logger.info(
            "job_claimed",
            job_id=str(job.id),
            version_id=str(job.version_id),
            document_id=job.document_id,
            attempt=job.attempts,
        )

        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job.id, stop_heartbeat))
        try:
            await self._execute_ingestion(job.id, job.version_id, job.document_id)
            return True
        except Exception as e:
            logger.error(
                "job_failed",
                job_id=str(job.id),
                version_id=str(job.version_id),
                document_id=job.document_id,
                error_type=type(e).__name__,
            )
            await self._handle_job_failure(job.id, job.version_id, f"Ingestion failed: {type(e).__name__}")
            return True
        finally:
            stop_heartbeat.set()
            await heartbeat

    async def _heartbeat(self, job_id: uuid.UUID, stop: asyncio.Event) -> None:
        interval = max(1, self.settings.worker_lease_duration_seconds // 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                async with async_session_maker() as session:
                    renewed = await JobRepository(session).update_heartbeat(
                        job_id, self.worker_id, self.settings.worker_lease_duration_seconds
                    )
                    await session.commit()
                    if not renewed:
                        return

    async def _execute_ingestion(
        self, job_id: uuid.UUID, version_id: uuid.UUID, document_id: str
    ) -> None:
        async with async_session_maker() as session:
            job_repo = JobRepository(session)
            document_repo = DocumentRepository(session)
            chunk_repo = ChunkRepository(session)

            version = await document_repo.get_version_by_id(version_id)
            if not version:
                raise ValueError(f"Version {version_id} not found.")
            if version.embedding_model_id != self.settings.embedding_identity:
                raise ValueError("Embedding model revision does not match this job version.")

            document = await document_repo.get_by_document_id(document_id, include_versions=False)
            if not document:
                raise ValueError(f"Document {document_id} not found.")

            # Keep the active version visible while it is being reindexed.
            if document.active_version_id != version_id:
                version.status = "PUBLISHING"
                await session.commit()

            # Initialize chunker with embedder tokenizer
            tokenizer = await asyncio.to_thread(
                lambda: getattr(self.embedder, "tokenizer", None)
            )
            if tokenizer is None:
                # Fallback simple word tokenizer if mock embedder without tokenizer
                from transformers import AutoTokenizer
                tokenizer = await asyncio.to_thread(
                    AutoTokenizer.from_pretrained,
                    "intfloat/multilingual-e5-small",
                )

            chunker = MarkdownChunker(
                tokenizer=tokenizer,
                target_tokens=self.settings.chunk_target_tokens,
                overlap_tokens=self.settings.chunk_overlap_tokens,
                max_tokens=self.settings.chunk_max_tokens,
            )

            # 1. Chunk document
            chunks = await asyncio.to_thread(
                chunker.chunk_document,
                document_id=document.document_id,
                version=version.version,
                title=version.metadata_snapshot["title"],
                visibility=version.metadata_snapshot["visibility"],
                locale=version.metadata_snapshot["locale"],
                raw_markdown=version.raw_markdown,
            )

            if not chunks:
                raise ValueError(f"No chunks generated from document {document_id}.")

            # 2. Embed passages locally
            passage_texts = [chunk.passage_text for chunk in chunks]
            embeddings = await asyncio.to_thread(self.embedder.embed_passages, passage_texts)

            if not await job_repo.owns_job(job_id, self.worker_id):
                raise RuntimeError("Worker lost the ingestion job lease.")

            # 3. Save chunks to pgvector
            await chunk_repo.save_chunks(
                version_id=version_id,
                document_id=document_id,
                chunk_items=chunks,
                embeddings=embeddings,
            )

            # An archive may have happened while the expensive embedding ran.
            await session.refresh(document)
            if document.status == "ARCHIVED":
                raise ValueError("Document was archived before ingestion completed.")

            # 4. Atomically activate new version (preserves old active version until this point!)
            await document_repo.activate_version(document_id=document_id, version_id=version_id)

            # 5. Mark job completed
            await job_repo.complete_job(job_id, self.worker_id)

            await session.commit()

            logger.info(
                "job_completed_successfully",
                job_id=str(job_id),
                document_id=document_id,
                version_id=str(version_id),
                chunk_count=len(chunks),
            )

    async def _handle_job_failure(
        self, job_id: uuid.UUID, version_id: uuid.UUID, error_message: str
    ) -> None:
        async with async_session_maker() as session:
            job_repo = JobRepository(session)
            document_repo = DocumentRepository(session)

            if not await job_repo.owns_job(job_id, self.worker_id, require_lease=False):
                return

            version = await document_repo.get_version_by_id(version_id)
            document = await document_repo.get_by_document_id(
                version.document_id, include_versions=False
            ) if version else None
            if version and document and document.status != "ARCHIVED" and document.active_version_id != version_id:
                version.status = "FAILED"

            await job_repo.fail_job(
                job_id=job_id, worker_id=self.worker_id, error_message=error_message
            )
            await session.commit()


async def main() -> None:
    worker = IngestionWorker()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            # Signal handling not supported on Windows loop
            pass

    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
