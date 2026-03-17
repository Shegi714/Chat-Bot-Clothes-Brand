# db.py
import os
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# По умолчанию локальная SQLite; для продакшена просто зададим переменную окружения:
# DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///app.db")

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

class Base(DeclarativeBase):
    pass

async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session

# Создаём фабрику асинхронных сессий поверх существующего async engine.
# В проекте engine уже объявлен (его импортируют в init_db/support_repo и т.д.).
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)