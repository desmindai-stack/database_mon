from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


def _async_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(_async_url(settings.database_url), echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def _sqlite_add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    cols = {row[1] for row in result.fetchall()}
    if column not in cols:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


async def migrate_schema() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    async with engine.begin() as conn:
        await _sqlite_add_column_if_missing(conn, "instances", "engine", "engine VARCHAR(32) DEFAULT 'postgresql'")
        await _sqlite_add_column_if_missing(conn, "instances", "options", "options JSON")
        await _sqlite_add_column_if_missing(conn, "instances", "customer_name", "customer_name VARCHAR(128)")
        await _sqlite_add_column_if_missing(conn, "instances", "environment", "environment VARCHAR(32) DEFAULT 'public'")
        await _sqlite_add_column_if_missing(conn, "instances", "application", "application VARCHAR(128)")
        await _sqlite_add_column_if_missing(conn, "instances", "cluster_name", "cluster_name VARCHAR(128)")
        await _sqlite_add_column_if_missing(conn, "instances", "role", "role VARCHAR(32)")
        await _sqlite_add_column_if_missing(conn, "instances", "services", "services JSON")
        await _sqlite_add_column_if_missing(
            conn, "metric_samples", "metrics_json", "metrics_json JSON"
        )


async def init_db() -> None:
    from pathlib import Path

    from app import models  # noqa: F401

    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_schema()
