# notify_repo.py
from __future__ import annotations
from sqlalchemy import text
from db import engine

# Создание таблицы (безопасно, если уже есть)
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notify_subs (
  user_id INTEGER PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Upsert по user_id — работает и в SQLite, и в Postgres (если PK/UNIQUE)
UPSERT_ON_SQLITE_PG = """
INSERT INTO notify_subs (user_id, enabled, created_at)
VALUES (:uid, 1, CURRENT_TIMESTAMP)
ON CONFLICT (user_id) DO UPDATE SET enabled=1
"""

SET_SQL = "UPDATE notify_subs SET enabled = :val WHERE user_id = :uid"
GET_SQL = "SELECT enabled FROM notify_subs WHERE user_id = :uid"

async def init_notify_table() -> None:
    async with engine.begin() as conn:
        await conn.execute(text(CREATE_TABLE_SQL))

async def ensure_notify_on(user_id: int) -> None:
    """Создаёт запись (если нет) или включает enabled=1 (если уже была)."""
    async with engine.begin() as conn:
        await conn.execute(text(UPSERT_ON_SQLITE_PG), {"uid": user_id})

async def set_subscription(user_id: int, enabled: bool) -> None:
    """Явно установить 1/0 (можно вызывать из кнопок)."""
    async with engine.begin() as conn:
        await conn.execute(text(SET_SQL), {"uid": user_id, "val": 1 if enabled else 0})

async def get_subscription(user_id: int) -> int:
    async with engine.begin() as conn:
        row = (await conn.execute(text(GET_SQL), {"uid": user_id})).first()
    return int(row[0]) if row else 0
