# agent_reviews.py
import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from calendar import monthrange

from sqlalchemy import text

from db import SessionLocal
from dao import (
    CURSOR_KEY_FEEDBACKS, get_cursor, set_cursor,
    upsert_review_from_wb,
)
from wb_feedbacks_api import fetch_feedbacks  # должен поддерживать date_from_ts и date_to_ts

LOG = logging.getLogger("agent_reviews")

# ==== Настройки загрузки WB ====
# Сколько дней истории тянем при первом запуске
WB_REVIEWS_LOOKBACK_DAYS = int(os.getenv("WB_REVIEWS_LOOKBACK_DAYS", "90"))
# Размер страницы (макс. 5000)
BATCH_TAKE = min(int(os.getenv("WB_FEEDBACK_TAKE", "5000")), 5000)
# Размер временного чанка (в днях). 1 день — надёжнее всего.
CHUNK_DAYS = int(os.getenv("WB_FEEDBACK_CHUNK_DAYS", "1"))

# ==== Настройки очистки БД ====
REVIEWS_TABLE = os.getenv("REVIEWS_TABLE_NAME", "reviews")
REVIEWS_DATE_COLUMN = os.getenv("REVIEWS_DATE_COLUMN", "created_at")
MONTHS_TO_KEEP = int(os.getenv("REVIEWS_MONTHS_TO_KEEP", "9"))  # по ТЗ — 9 месяцев


# =========================
# ВСПОМОГАТЕЛЬНЫЕ ДАТЫ/ВРЕМЯ
# =========================
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _months_ago(dt: datetime, months: int) -> datetime:
    """
    Возвращает dt минус N месяцев, корректируя дни конца месяца.
    Пример: 31.10 - 9м = 31.01 → подрежется до 31 (или 30/29/28) по календарю.
    """
    y, m = dt.year, dt.month
    m -= months
    while m <= 0:
        m += 12
        y -= 1
    d = min(dt.day, monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


# =========================
# ПАРСИНГ ДАТ И WB-ВСПОМОГАТЕЛЬНОЕ
# =========================
def _start_ts_initial() -> int:
    """Если курсора нет — стартуем now - LOOKBACK, 00:00 UTC."""
    start = (_now_utc() - timedelta(days=WB_REVIEWS_LOOKBACK_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(start.timestamp())

def _parse_created_ts(cd: str | None) -> int | None:
    """
    Парсим createdDate из WB:
    - '...Z' → UTC
    - без TZ → считаем МСК (+03:00), приводим к UTC
    - с TZ → используем как есть
    """
    if not cd:
        return None
    try:
        s = cd.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
        return int(dt.astimezone(timezone.utc).timestamp())
    except Exception:
        return None


# =========================
# ЗАГРУЗКА ОТЗЫВОВ ИЗ WB (как у тебя)
# =========================
async def _process_interval(start_ts: int, end_ts: int) -> tuple[int, int]:
    """
    Качаем отзывы в интервале [start_ts, end_ts), пагинируя по skip.
    Возвращаем (saved_count, max_seen_ts). max_seen_ts >= start_ts.
    """
    total_saved = 0
    max_seen_ts = start_ts

    for answered in (False, True):
        skip = 0
        while True:
            print(f"[reviews-agent] req isAnswered={answered} take={BATCH_TAKE} skip={skip} "
                  f"from={start_ts} to={end_ts}")
            resp = await fetch_feedbacks(
                is_answered=answered,
                take=BATCH_TAKE,
                skip=skip,
                order="dateAsc",
                date_from_ts=start_ts,
                date_to_ts=end_ts
            )
            data = (resp or {}).get("data") or {}
            items = data.get("feedbacks") or []

            # Диагностика — сколько WB говорит есть всего
            if skip == 0:
                cu = data.get("countUnanswered")
                ca = data.get("countArchive")
                if cu is not None or ca is not None:
                    print(f"[reviews-agent] counters isAnswered={answered}: "
                          f"countUnanswered={cu}, countArchive={ca}")

            if not items:
                break

            # Апсертим пачку
            async with SessionLocal() as session:
                for fb in items:
                    await upsert_review_from_wb(session, fb)
                    ts = _parse_created_ts(fb.get("createdDate"))
                    if ts and ts > max_seen_ts:
                        max_seen_ts = ts

            batch_saved = len(items)
            total_saved += batch_saved
            print(f"[reviews-agent] сохранено {batch_saved} отзывов (acc={total_saved})")

            if batch_saved < BATCH_TAKE:
                break
            skip += BATCH_TAKE

    return total_saved, max_seen_ts

async def run_feedbacks_sync_once() -> None:
    # читаем курсор
    async with SessionLocal() as s0:
        cursor_str = await get_cursor(s0, CURSOR_KEY_FEEDBACKS)

    if cursor_str is None:
        base_ts = _start_ts_initial()
        print(f"[reviews-agent] курсора не было, стартуем с ts={base_ts} (UTC, -{WB_REVIEWS_LOOKBACK_DAYS}d)")
    else:
        base_ts = int(cursor_str)
        print(f"[reviews-agent] продолжаем с ts={base_ts}")

    # идём чанками от base_ts до now
    end_ts_global = int(_now_utc().timestamp())

    total_saved_all = 0
    max_cursor_committed = base_ts

    cur_dt = datetime.fromtimestamp(base_ts, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts_global, tz=timezone.utc)

    while cur_dt < end_dt:
        nxt_dt = min(cur_dt + timedelta(days=CHUNK_DAYS), end_dt)
        s_ts = int(cur_dt.timestamp())
        e_ts = int(nxt_dt.timestamp())

        try:
            saved, max_seen = await _process_interval(s_ts, e_ts)
            total_saved_all += saved

            # Выберем новый курсор:
            # - если что-то увидели в чанке → на макс. дату созданного
            # - если ничего не было → на конец чанка (чтобы не застрять)
            if saved > 0 and max_seen >= s_ts:
                new_cursor = max_seen
            else:
                new_cursor = e_ts

            # Фиксируем курсор после успешного чанка
            async with SessionLocal() as session:
                await set_cursor(session, CURSOR_KEY_FEEDBACKS, str(new_cursor))
            max_cursor_committed = new_cursor

            print(f"[reviews-agent] chunk {cur_dt.isoformat()} → {nxt_dt.isoformat()} "
                  f"saved={saved}, max_seen_ts={max_seen}, cursor→{new_cursor}")

            # следующий чанк
            cur_dt = nxt_dt

        except Exception as e:
            # Ошибка в текущем чанке — КУРСОР НЕ ДВИГАЕМ, выходим.
            print(f"[reviews-agent] ошибка запроса/обработки: {e}")
            break

    print(f"[reviews-agent] итог: acc_all={total_saved_all}, cursor={max_cursor_committed}")


# =========================
# ОЧИСТКА СТАРЫХ ОТЗЫВОВ (> 9 месяцев)
# =========================
async def cleanup_old_reviews_once() -> int:
    """
    Удаляет из REVIEWS_TABLE все записи, где REVIEWS_DATE_COLUMN < (сегодня - MONTHS_TO_KEEP месяцев).
    Пример: при 27.10.2025 удалим всё строго раньше 27.01.2025.
    """
    now = _now_utc()
    cutoff = _months_ago(now, MONTHS_TO_KEEP)

    async with SessionLocal() as session:
        res = await session.execute(
            text(f"DELETE FROM {REVIEWS_TABLE} WHERE {REVIEWS_DATE_COLUMN} < :cutoff"),
            {"cutoff": cutoff}
        )
        await session.commit()

    deleted = res.rowcount or 0
    LOG.info(
        "Reviews cleanup done: cutoff=%s, deleted=%s",
        cutoff.isoformat(), deleted
    )
    return deleted


# =========================
# ЕЖЕДНЕВНЫЙ АГЕНТ (синк + очистка)
# =========================
async def daily_reviews_agent():
    while True:
        try:
            print("[reviews-agent] старт синхронизации...")
            await run_feedbacks_sync_once()
            print("[reviews-agent] синхронизация завершена")
        except Exception as e:
            print(f"[reviews-agent] ошибка синхронизации: {e}")

        try:
            deleted = await cleanup_old_reviews_once()
            print(f"[reviews-agent] очистка завершена, удалено {deleted} записей старше {MONTHS_TO_KEEP} мес.")
        except Exception as e:
            print(f"[reviews-agent] ошибка очистки: {e}")

        await asyncio.sleep(24 * 60 * 60)


if __name__ == "__main__":
    asyncio.run(run_feedbacks_sync_once())
