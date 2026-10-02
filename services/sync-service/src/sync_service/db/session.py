from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    """Cria o engine async. Não abre conexão até o primeiro uso."""
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 5},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
