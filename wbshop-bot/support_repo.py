# support_repo.py
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from sqlalchemy import text
from db import engine  # используем общий engine

TBL = "support_tickets"


def _parse_datetime(value: Any) -> datetime:
    """
    Преобразует значение из БД в datetime объект.
    SQLite может возвращать TIMESTAMP как строку, поэтому нужно преобразование.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # Пробуем ISO формат (2024-01-22T19:21:23.762000+00:00)
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            # Пробуем другие форматы
            for fmt in ['%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']:
                try:
                    return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
        raise ValueError(f"Не удалось преобразовать '{value}' в datetime")
    raise TypeError(f"Неподдерживаемый тип для преобразования в datetime: {type(value)}")

DDL = f"""
CREATE TABLE IF NOT EXISTS {TBL} (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT UNIQUE NOT NULL,
  user_id INTEGER NOT NULL,
  thread_id INTEGER UNIQUE NOT NULL,
  status TEXT NOT NULL,
  general_msg_id INTEGER,
  card_msg_id INTEGER NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{TBL}_user ON {TBL}(user_id);
CREATE INDEX IF NOT EXISTS idx_{TBL}_status ON {TBL}(status);
"""

async def init_tables():
    async with engine.begin() as conn:
        # SQLite спокойно выполнит несколько CREATE IF NOT EXISTS подряд
        for stmt in DDL.strip().split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))

async def insert_ticket(
    ticket_id: str,
    user_id: int,
    thread_id: int,
    status: str,
    general_msg_id: Optional[int],
    card_msg_id: int
):
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await conn.execute(
            text(f"""
                INSERT OR REPLACE INTO {TBL}
                (ticket_id, user_id, thread_id, status, general_msg_id, card_msg_id, created_at, updated_at)
                VALUES (:ticket_id, :user_id, :thread_id, :status, :general_msg_id, :card_msg_id, :created_at, :updated_at)
            """),
            dict(
                ticket_id=ticket_id,
                user_id=user_id,
                thread_id=thread_id,
                status=status,
                general_msg_id=general_msg_id,
                card_msg_id=card_msg_id,
                created_at=now,
                updated_at=now,
            )
        )

async def update_ticket(ticket_id: str, **fields: Any):
    if not fields:
        return
    if "updated_at" not in fields:
        fields["updated_at"] = datetime.now(timezone.utc)
    sets = ", ".join([f"{k}=:{k}" for k in fields.keys()])
    async with engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE {TBL} SET {sets} WHERE ticket_id=:ticket_id"),
            dict(ticket_id=ticket_id, **fields)
        )

async def get_by_ticket(ticket_id: str) -> Optional[Dict[str, Any]]:
    async with engine.begin() as conn:
        res = await conn.execute(
            text(f"SELECT * FROM {TBL} WHERE ticket_id=:ticket_id"),
            dict(ticket_id=ticket_id)
        )
        row = res.mappings().first()
        return dict(row) if row else None

async def get_by_thread(thread_id: int) -> Optional[Dict[str, Any]]:
    async with engine.begin() as conn:
        res = await conn.execute(
            text(f"SELECT * FROM {TBL} WHERE thread_id=:thread_id"),
            dict(thread_id=thread_id)
        )
        row = res.mappings().first()
        return dict(row) if row else None

async def get_active_for_user(user_id: int, ttl_minutes: int) -> Optional[Dict[str, Any]]:
    """
    Возвращает последний НЕ CLOSED тикет пользователя, если он «свежий» по TTL.
    Это вспомогательная функция и может использоваться для сценариев с коротким окном активности.
    """
    async with engine.begin() as conn:
        res = await conn.execute(
            text(f"""
                SELECT * FROM {TBL}
                WHERE user_id=:user_id AND status!='CLOSED'
                ORDER BY updated_at DESC
                LIMIT 1
            """),
            dict(user_id=user_id)
        )
        row = res.mappings().first()
        if not row:
            return None
        data = dict(row)
        updated_at = _parse_datetime(data["updated_at"])
        age = datetime.now(timezone.utc) - updated_at
        return data if age <= timedelta(minutes=ttl_minutes) else None

async def get_current_for_user(user_id: int, autoclose_hours: int) -> Optional[Dict[str, Any]]:
    """
    Возвращает 'текущий' тикет пользователя, который надо продолжать:
    - если есть НЕ CLOSED со статусом OPEN / PENDING_USER — берём самый свежий, без ограничений по времени;
    - иначе если есть RESOLVED и с момента updated_at прошло меньше autoclose_hours — берём его;
    - иначе None (создаём новый).
    """
    now = datetime.now(timezone.utc)

    async with engine.begin() as conn:
        # 1) OPEN / PENDING_USER — без ограничения по времени
        res = await conn.execute(
            text(f"""
                SELECT * FROM {TBL}
                WHERE user_id=:user_id AND status IN ('OPEN','PENDING_USER')
                ORDER BY updated_at DESC
                LIMIT 1
            """),
            dict(user_id=user_id)
        )
        row = res.mappings().first()
        if row:
            return dict(row)

        # 2) RESOLVED — только в окне авто-закрытия
        res = await conn.execute(
            text(f"""
                SELECT * FROM {TBL}
                WHERE user_id=:user_id AND status='RESOLVED'
                ORDER BY updated_at DESC
                LIMIT 1
            """),
            dict(user_id=user_id)
        )
        row = res.mappings().first()
        if row:
            data = dict(row)
            updated_at = _parse_datetime(data["updated_at"])
            age = now - updated_at
            if age <= timedelta(hours=autoclose_hours):
                return data

    return None
