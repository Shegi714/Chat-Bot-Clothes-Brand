# scripts/fill_amount_from_wb.py
"""
Заполняет orders.amount_rub из Wildberries Statistics API: поле finishedPrice (в РУБЛЯХ, БЕЗ деления на 100).

Запуск:
  python scripts/amount_upd.py --dry-run     # посмотреть, что обновится (без commit)
  python scripts/amount_upd.py               # обновить

Параметры:
  --since-days N   для заказов без даты: диапазон «сегодня - N дней» (по умолчанию 365)
  --limit N        ограничить число записей из БД
  --dry-run        не писать в БД
  --batch-size N   размер батча коммитов (по умолчанию 500)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Добавляем родительскую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import asyncio
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List

import aiohttp
from dotenv import load_dotenv
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

# грузим .env
load_dotenv()

# --- DB фабрика сессий ---
try:
    from db import async_session_maker  # type: ignore
except Exception:
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from db import engine  # type: ignore
    async_session_maker = async_sessionmaker(engine, expire_on_commit=False)  # type: ignore

from models import Order  # type: ignore

WB_STAT_BASE = os.getenv("WB_STAT_BASE", "https://statistics-api.wildberries.ru")
WB_API_KEY = os.getenv("WB_API_KEY", "").strip()
if not WB_API_KEY:
    raise RuntimeError("WB_API_KEY не задан в .env — укажите токен поставщика Wildberries")


def _quant_rub(v) -> Optional[Decimal]:
    """Преобразуем значение в Decimal(рубли, 2 знака). Принимаем только > 0."""
    if v is None:
        return None
    try:
        d = Decimal(str(v))
        if d <= 0:
            return None
        return d.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _finished_price_rub(it: Dict[str, Any]) -> Optional[Decimal]:
    """
    Берём ТОЛЬКО finishedPrice (уже в рублях, БЕЗ деления на 100).
    Если значения нет или <= 0 — пропускаем.
    """
    return _quant_rub(it.get("finishedPrice"))


def _to_iso_day(d: date) -> str:
    # Statistics API принимает ISO8601; используем полночь UTC (WB понимает)
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc).isoformat()


async def _fetch_wb_orders(session_http: aiohttp.ClientSession, date_from_iso: str) -> List[Dict[str, Any]]:
    """
    /api/v1/supplier/orders?dateFrom=...
    Возвращает список заказов WB (list[dict]).
    """
    url = f"{WB_STAT_BASE}/api/v1/supplier/orders"
    headers = {"Authorization": WB_API_KEY}
    params = {"dateFrom": date_from_iso}
    for attempt in range(5):
        try:
            async with session_http.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 429:
                    await asyncio.sleep(1 + attempt * 2)
                    continue
                r.raise_for_status()
                data = await r.json(content_type=None)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    return data["data"]
                return []
        except Exception:
            await asyncio.sleep(1 + attempt)
    return []


async def _srid_to_finished_price_for_day(session_http: aiohttp.ClientSession, d: date) -> Dict[str, Decimal]:
    iso_from = _to_iso_day(d)
    items = await _fetch_wb_orders(session_http, iso_from)
    out: Dict[str, Decimal] = {}
    for it in items:
        srid = it.get("srid") or it.get("sr_id") or it.get("srId")
        if not srid:
            continue
        price = _finished_price_rub(it)
        if price is None:
            continue
        out[str(srid)] = price
    return out


async def _srid_to_finished_price_since(session_http: aiohttp.ClientSession, since_days: int) -> Dict[str, Decimal]:
    start = (datetime.now(timezone.utc) - timedelta(days=since_days)).date()
    iso_from = _to_iso_day(start)
    items = await _fetch_wb_orders(session_http, iso_from)
    out: Dict[str, Decimal] = {}
    for it in items:
        srid = it.get("srid") or it.get("sr_id") or it.get("srId")
        if not srid:
            continue
        price = _finished_price_rub(it)
        if price is None:
            continue
        out[str(srid)] = price
    return out


async def _select_targets(db: AsyncSession, limit: Optional[int]) -> List[Order]:
    cond = or_(Order.amount_rub.is_(None), Order.amount_rub == 0)
    stmt = select(Order).where(cond).order_by(Order.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def main():
    parser = argparse.ArgumentParser(description="Fill orders.amount_rub from WB finishedPrice (RUB, no division)")
    parser.add_argument("--since-days", type=int, default=365, help="Для заказов без даты: диапазон «сегодня - N дней»")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число записей из БД")
    parser.add_argument("--dry-run", action="store_true", help="Не коммитить изменения в БД")
    parser.add_argument("--batch-size", type=int, default=500, help="Размер батча commit")
    args = parser.parse_args()

    async with async_session_maker() as db, aiohttp.ClientSession() as http:
        orders = await _select_targets(db, args.limit)
        if not orders:
            print("[fill] нет записей для обновления")
            return

        # группируем по дате для точности/экономии трафика
        from collections import defaultdict
        by_day = defaultdict(list)
        no_date: List[Order] = []
        for o in orders:
            od = getattr(o, "date", None)
            if od:
                by_day[od.date()].append(o)
            else:
                no_date.append(o)

        updated = 0
        checked = 0

        # 1) заказы с датой — точечно по дням
        for d, chunk in by_day.items():
            srid_price = await _srid_to_finished_price_for_day(http, d)
            for o in chunk:
                checked += 1
                srid = str(getattr(o, "srid", "") or "")
                if not srid:
                    continue
                price = srid_price.get(srid)
                if price is None:
                    continue
                if getattr(o, "amount_rub", None):
                    continue
                setattr(o, "amount_rub", float(price))  # Decimal → float для совместимости
                updated += 1
                if not args.dry_run and (updated % args.batch_size == 0):
                    await db.commit()

        # 2) заказы без даты — общим диапазоном
        if no_date:
            srid_price = await _srid_to_finished_price_since(http, args.since_days)
            for o in no_date:
                checked += 1
                srid = str(getattr(o, "srid", "") or "")
                if not srid:
                    continue
                price = srid_price.get(srid)
                if price is None:
                    continue
                if getattr(o, "amount_rub", None):
                    continue
                setattr(o, "amount_rub", float(price))
                updated += 1
                if not args.dry_run and (updated % args.batch_size == 0):
                    await db.commit()

        if not args.dry_run and updated:
            await db.commit()

        print(f"[fill] checked={checked} ; updated={updated} ; dry_run={args.dry_run}")


if __name__ == "__main__":
    asyncio.run(main())

