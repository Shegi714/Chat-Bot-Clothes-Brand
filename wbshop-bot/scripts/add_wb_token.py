# scripts/add_wb_token.py
"""
Скрипт для добавления / обновления токена Wildberries в таблицу wb_tokens.
Просто подставь ниже свои значения и запусти:
    python -m scripts.add_wb_token
    или из корня wbshop-bot: python scripts/add_wb_token.py
"""

import sys
from pathlib import Path

# Добавляем родительскую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from sqlalchemy import text
from wbshop_bot.storage.db import engine  # импортируем ваш async engine
from wbshop_bot.storage.secrets_util import enc  # если не используете шифрование — можно заменить на lambda s: s

# ======== НАСТРОЙКИ ==========
ALIAS = "shop-2"          # короткое имя кабинета (например: shop-main, wb2 и т.п.)
TOKEN = "вставьте токен"  # сам токен WB
ACTIVE = True              # True = активен, False = отключён
# ==============================


async def add_token(alias: str, token: str, active: bool = True):
    token_enc = enc(token)
    async with engine.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO wb_tokens (alias, token_enc, active)
                VALUES (:alias, :token_enc, :active)
                ON CONFLICT(alias)
                DO UPDATE SET token_enc = excluded.token_enc,
                              active = excluded.active,
                              added_at = CURRENT_TIMESTAMP
            """),
            {"alias": alias, "token_enc": token_enc, "active": 1 if active else 0}
        )
    print(f"✅ Токен '{alias}' успешно {'добавлен' if active else 'обновлён'} в базу.")


if __name__ == "__main__":
    asyncio.run(add_token(ALIAS, TOKEN, ACTIVE))

