from collections.abc import AsyncIterator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from homespace_ai.core.config import get_settings


class Base(DeclarativeBase):
    pass


from sqlalchemy.pool import NullPool

settings = get_settings()

engine = create_async_engine(
    settings.ai_database_url,
    echo=(settings.log_level.upper() == "DEBUG"),
    poolclass=NullPool,
    pool_pre_ping=True,
)

async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
