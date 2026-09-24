from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from homespace_ai.models.knowledge import KnowledgeIngestionJob, WorkerHeartbeat


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, job_id: uuid.UUID) -> KnowledgeIngestionJob | None:
        stmt = (
            select(KnowledgeIngestionJob)
            .where(KnowledgeIngestionJob.id == job_id)
            .options(selectinload(KnowledgeIngestionJob.version))
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_active_job_for_version(
        self, version_id: uuid.UUID
    ) -> KnowledgeIngestionJob | None:
        """Finds any currently QUEUED or actively RUNNING job for this version."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(KnowledgeIngestionJob)
            .where(
                KnowledgeIngestionJob.version_id == version_id,
                or_(
                    KnowledgeIngestionJob.status == "QUEUED",
                    (
                        (KnowledgeIngestionJob.status == "RUNNING")
                        & (KnowledgeIngestionJob.lease_until > now)
                    ),
                ),
            )
            .order_by(KnowledgeIngestionJob.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_latest_job_for_version(
        self, version_id: uuid.UUID
    ) -> KnowledgeIngestionJob | None:
        stmt = (
            select(KnowledgeIngestionJob)
            .where(KnowledgeIngestionJob.version_id == version_id)
            .order_by(KnowledgeIngestionJob.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def create_job(
        self,
        version_id: uuid.UUID,
        document_id: str,
        job_type: str = "INGEST",
        max_attempts: int = 3,
    ) -> KnowledgeIngestionJob:
        job = KnowledgeIngestionJob(
            version_id=version_id,
            document_id=document_id,
            job_type=job_type,
            status="QUEUED",
            attempts=0,
            max_attempts=max_attempts,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def claim_next_job(
        self, worker_id: str, lease_seconds: int = 60
    ) -> KnowledgeIngestionJob | None:
        """Atomically claims the next job using FOR UPDATE SKIP LOCKED.
        Handles both new QUEUED jobs and orphaned/stale RUNNING jobs whose lease has expired.
        """
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=lease_seconds)

        await self.session.execute(
            update(KnowledgeIngestionJob)
            .where(
                KnowledgeIngestionJob.status == "RUNNING",
                KnowledgeIngestionJob.lease_until <= now,
                KnowledgeIngestionJob.attempts >= KnowledgeIngestionJob.max_attempts,
            )
            .values(status="FAILED", lease_until=None, worker_id=None, updated_at=now)
        )

        stmt = (
            select(KnowledgeIngestionJob)
            .where(
                KnowledgeIngestionJob.attempts < KnowledgeIngestionJob.max_attempts,
                or_(
                    KnowledgeIngestionJob.status == "QUEUED",
                    (
                        (KnowledgeIngestionJob.status == "RUNNING")
                        & (KnowledgeIngestionJob.lease_until <= now)
                    ),
                ),
            )
            .order_by(KnowledgeIngestionJob.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        job = result.scalars().first()

        if job:
            job.status = "RUNNING"
            job.worker_id = worker_id
            job.lease_until = lease_until
            job.attempts += 1
            job.updated_at = now
            await self.session.flush()

        return job

    async def update_heartbeat(
        self, job_id: uuid.UUID, worker_id: str, lease_seconds: int = 60
    ) -> bool:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=lease_seconds)
        stmt = (
            update(KnowledgeIngestionJob)
            .where(
                KnowledgeIngestionJob.id == job_id,
                KnowledgeIngestionJob.worker_id == worker_id,
                KnowledgeIngestionJob.status == "RUNNING",
            )
            .values(lease_until=lease_until, updated_at=now)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def owns_job(
        self, job_id: uuid.UUID, worker_id: str, require_lease: bool = True
    ) -> bool:
        """Lock the job row before a worker commits results for it."""
        stmt = (
            select(KnowledgeIngestionJob)
            .where(KnowledgeIngestionJob.id == job_id)
            .with_for_update()
        )
        job = (await self.session.execute(stmt)).scalars().first()
        return bool(
            job
            and job.status == "RUNNING"
            and job.worker_id == worker_id
            and (not require_lease or (
                job.lease_until is not None
                and job.lease_until > datetime.now(timezone.utc)
            ))
        )

    async def complete_job(self, job_id: uuid.UUID, worker_id: str) -> None:
        now = datetime.now(timezone.utc)
        await self.session.execute(
            update(KnowledgeIngestionJob)
            .where(KnowledgeIngestionJob.id == job_id, KnowledgeIngestionJob.worker_id == worker_id)
            .values(
                status="COMPLETED",
                lease_until=None,
                error_message=None,
                updated_at=now,
            )
        )
        await self.session.flush()

    async def fail_job(self, job_id: uuid.UUID, worker_id: str, error_message: str) -> None:
        now = datetime.now(timezone.utc)
        job = (await self.session.execute(
            select(KnowledgeIngestionJob)
            .where(KnowledgeIngestionJob.id == job_id)
            .with_for_update()
        )).scalars().first()
        if job and job.worker_id == worker_id and job.status == "RUNNING":
            job.status = "QUEUED" if job.attempts < job.max_attempts else "FAILED"
            job.worker_id = None
            job.lease_until = None
            job.error_message = error_message[:1000]
            job.updated_at = now
        await self.session.flush()

    async def record_worker_heartbeat(
        self, worker_id: str, hostname: str, pid: int
    ) -> None:
        now = datetime.now(timezone.utc)
        stmt = select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id)
        existing = (await self.session.execute(stmt)).scalars().first()
        if existing:
            existing.last_heartbeat = now
            existing.status = "ONLINE"
        else:
            hb = WorkerHeartbeat(
                worker_id=worker_id,
                hostname=hostname,
                pid=pid,
                status="ONLINE",
                last_heartbeat=now,
                started_at=now,
            )
            self.session.add(hb)
        await self.session.flush()

    async def unregister_worker(self, worker_id: str) -> None:
        await self.session.execute(
            update(WorkerHeartbeat)
            .where(WorkerHeartbeat.worker_id == worker_id)
            .values(status="OFFLINE")
        )
        await self.session.flush()

    async def get_active_workers(self, timeout_seconds: int = 45) -> list[WorkerHeartbeat]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        stmt = (
            select(WorkerHeartbeat)
            .where(
                WorkerHeartbeat.status == "ONLINE",
                WorkerHeartbeat.last_heartbeat >= cutoff,
            )
            .order_by(WorkerHeartbeat.last_heartbeat.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

