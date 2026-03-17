# scripts/init_db.py
"""
Скрипт для инициализации базы данных.
Создает все ORM-модели и служебные таблицы (wb_tokens, wb_cursors).
"""
import sys
from pathlib import Path

# Добавляем родительскую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from sqlalchemy import text
from db import engine
from models import Base

DDL_TOKENS = """
CREATE TABLE IF NOT EXISTS wb_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alias TEXT NOT NULL UNIQUE,
  token_enc TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

DDL_CURSORS = """
CREATE TABLE IF NOT EXISTS wb_cursors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_id INTEGER NOT NULL,
  kind TEXT NOT NULL,         -- 'orders' | 'reviews'
  cursor TEXT,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(token_id, kind)
);
"""

async def init_models():
    async with engine.begin() as conn:
        # создаём все ORM-модели
        await conn.run_sync(Base.metadata.create_all)
        # создаём служебные таблицы под токены/курсоры (если их нет в моделях)
        await conn.execute(text(DDL_TOKENS))
        await conn.execute(text(DDL_CURSORS))
    print("DB initialized: tables created (ORM + WB service tables).")

if __name__ == "__main__":
    asyncio.run(init_models())

