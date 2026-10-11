"""Run filename/version integration tests in a newly created disposable PostgreSQL database.

Never resets the application's database. No provider or embedding download calls.
"""
import asyncio
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import asyncpg
from sqlalchemy.engine import make_url

from homespace_ai.core.config import get_settings


async def main() -> int:
    url = make_url(get_settings().ai_database_url)
    test_name = f"hs_knowledge_{uuid.uuid4().hex[:12]}_test"
    if not re.fullmatch(r"hs_knowledge_[a-f0-9]{12}_test", test_name):
        raise RuntimeError("Unsafe generated test database name.")
    admin = await asyncpg.connect(url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False))
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{test_name}"')
        created = True
        env = {**os.environ, "AI_DATABASE_URL": url.set(database=test_name).render_as_string(hide_password=False),
               "HS_RUN_RAG_INTEGRATION": "1", "GENERATION_PROVIDER": "disabled"}
        root = Path(__file__).resolve().parents[1]
        legacy = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "alembic", "upgrade", "919dda8816a3"], cwd=root, env=env)
        if legacy.returncode:
            return legacy.returncode
        test_connection = await asyncpg.connect(url.set(drivername="postgresql", database=test_name).render_as_string(hide_password=False))
        try:
            for index, filename in enumerate(["Legacy-Unique.MD", "legacy-duplicate.md", "legacy-duplicate.md"]):
                import json
                doc_id = f"legacy-test-{index}"
                await test_connection.execute("""
                    INSERT INTO knowledge_documents (id, document_id, title, locale, visibility, audience, status)
                    VALUES ($1, $2, 'Legacy', 'vi-VN', 'public', '["tenant"]', 'APPROVED')
                """, uuid.uuid4(), doc_id)
                for number in range(2):
                    await test_connection.execute("""
                        INSERT INTO knowledge_document_versions
                        (id, document_id, version, raw_markdown, content_hash, metadata_snapshot, embedding_model_id, chunk_config_version, status, created_at)
                        VALUES ($1, $2, '0.1.0', '# Old content', 'same-hash', $3::jsonb, 'legacy', 'v1', 'PUBLISHED', now() + $4 * interval '1 second')
                    """, uuid.uuid4(), doc_id, json.dumps({"original_filename": filename, "title": "Legacy", "visibility": "public", "audience": ["tenant"], "locale": "vi-VN"}), number)
        finally:
            await test_connection.close()
        migrated = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env)
        if migrated.returncode:
            return migrated.returncode
        tested = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "pytest", "tests/integration/test_managed_knowledge.py", "-q"], cwd=root, env=env)
        return tested.returncode
    finally:
        if created:
            await admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid <> pg_backend_pid()", test_name)
            await admin.execute(f'DROP DATABASE "{test_name}"')
            print("Disposable test database removed; application data was not reset.")
        await admin.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
