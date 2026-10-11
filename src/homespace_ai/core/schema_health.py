"""Fail early when the runtime database has not received the RAG migrations."""
import asyncio

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from homespace_ai.core.database import Base
import homespace_ai.models  # noqa: F401


class SchemaNotReadyError(RuntimeError):
    pass


def check_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    missing = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            missing.append(table.name)
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in columns)
    if missing:
        raise SchemaNotReadyError(
            "AI database schema is incomplete: " + ", ".join(missing)
            + ". Run `uv run alembic upgrade head` in hs-ai-service against the same AI_DATABASE_URL, then restart API and worker."
        )


async def ensure_schema_ready(engine: AsyncEngine) -> None:
    async with asyncio.timeout(10):
        async with engine.connect() as connection:
            await connection.run_sync(check_schema)
