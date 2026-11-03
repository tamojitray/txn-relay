import ssl
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base


def _asyncpg_url_and_connect_args(database_url: str) -> tuple[URL, dict]:
    """Convert a standard Supabase/Postgres URI into an asyncpg SQLAlchemy URL."""

    url = make_url(database_url)
    if not url.drivername.startswith("postgresql") and url.drivername != "postgres":
        raise ValueError("DATABASE_URL must be a PostgreSQL connection string")

    query = dict(url.query)
    ssl_mode = query.pop("sslmode", None) or query.pop("ssl", None)
    url = url.set(drivername="postgresql+asyncpg", query=query)

    connect_args: dict = {"server_settings": {"application_name": "transaction-webhook"}}
    if ssl_mode and ssl_mode.lower() not in {"disable", "false", "0"}:
        connect_args["ssl"] = ssl.create_default_context()
    return url, connect_args


class Database:
    def __init__(self, database_url: str | None) -> None:
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is not configured. Copy .env.example to .env and add "
                "the Supabase Postgres connection string."
            )

        url, connect_args = _asyncpg_url_and_connect_args(database_url)
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            connect_args=connect_args,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def initialize(self, auto_create_schema: bool) -> None:
        if auto_create_schema:
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        else:
            await self.ping()

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
