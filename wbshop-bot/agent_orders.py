# agent_orders.py
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from calendar import monthrange

from sqlalchemy import text
from db import engine  # AsyncEngine для SQL очистки

from wb_api import get_active_tokens, get_cursor, set_cursor, fetch_orders_page
from dao import upsert_orders  # idempotent пакетная вставка/апдейт заказов

LOG = logging.getLogger("agent_orders")

# ==== Настройки очистки ====
TABLE = "orders"
DATE_COLUMN = "created_at"     # Переименуйте при необходимости
MONTHS_TO_KEEP = 9             # Храним 9 месяцев, старше — удаляем


# =========================
# ВЫГРУЗКА ЗАКАЗОВ (ваш текущий функционал)
# =========================
async def process_orders_for_token(token_id: int, token: str, alias: str):
    """
    Проходит заказы постранично для одного токена, поддерживая курсор per-token.
    """
    cursor = await get_cursor(token_id, "orders")
    while True:
        data = await fetch_orders_page(token=token, cursor=cursor)
        items: List[Dict[str, Any]] = data.get("items", [])
        if items:
            await upsert_orders(items)

        next_cursor: Optional[str] = data.get("next_cursor")
        if next_cursor:
            await set_cursor(token_id, "orders", next_cursor)
            cursor = next_cursor

        if not data.get("has_more"):
            break


async def run_orders_agent():
    """
    Основной раннер: обходит все активные токены и качает заказы.
    """
    tokens = await get_active_tokens()
    for token_id, token, alias in tokens:
        try:
            await process_orders_for_token(token_id, token, alias)
        except Exception as e:
            # логируем и идём дальше, чтобы сбой одного токена не ломал остальных
            LOG.exception("[orders] token=%s error=%s", alias, e)


# =========================
# ОЧИСТКА СТАРЫХ ЗАПИСЕЙ (> 9 месяцев)
# =========================
def _months_ago(dt: datetime, months: int) -> datetime:
    """
    Возвращает дату/время dt минус N месяцев.
    Корректно подрезает дни для конца месяца.
    """
    y, m = dt.year, dt.month
    m -= months
    while m <= 0:
        m += 12
        y -= 1
    d = min(dt.day, monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


async def cleanup_old_orders_once() -> int:
    """
    Удаляет из TABLE все записи, где DATE_COLUMN < (сегодня - MONTHS_TO_KEEP месяцев).
    Пример: при 27.10.2025 удалим всё строго раньше 27.01.2025.
    """
    now = datetime.now(timezone.utc)
    cutoff = _months_ago(now, MONTHS_TO_KEEP)

    async with engine.begin() as conn:
        res = await conn.execute(
            text(f"DELETE FROM {TABLE} WHERE {DATE_COLUMN} < :cutoff"),
            {"cutoff": cutoff}
        )
    deleted = res.rowcount or 0
    LOG.info(
        "Orders cleanup done: cutoff=%s, deleted=%s",
        cutoff.isoformat(), deleted
    )
    return deleted


# =========================
# ЕЖЕДНЕВНЫЙ АГЕНТ (выгрузка + очистка)
# =========================
async def daily_orders_agent():
    """
    Раз в сутки:
      1) запускает выгрузку заказов по всем токенам,
      2) чистит старые записи в orders,
      3) ожидает 24 часа.
    Остаётся обратно-совместимым названием для main.py.
    """
    LOG.info("daily_orders_agent started")
    while True:
        try:
            await run_orders_agent()
        except asyncio.CancelledError:
            LOG.info("daily_orders_agent cancelled, exiting…")
            raise
        except Exception:
            LOG.exception("Orders fetch failed in daily loop")

        try:
            await cleanup_old_orders_once()
        except asyncio.CancelledError:
            LOG.info("daily_orders_agent cancelled during cleanup, exiting…")
            raise
        except Exception:
            LOG.exception("Orders cleanup failed in daily loop")

        await asyncio.sleep(24 * 60 * 60)


if __name__ == "__main__":
    # Одноразовый прогон для ручного запуска:
    asyncio.run(run_orders_agent())
