# scripts/backfill_wb_tokens.py
from __future__ import annotations
import sys
from pathlib import Path

# Добавляем родительскую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from wb_api import (
    get_active_tokens,
    fetch_orders_range,
    fetch_reviews_range,
)
from dao import upsert_orders, upsert_reviews


# -------- helpers --------
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _ensure_aware_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return _utc_now()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# -------- core backfill --------
async def backfill_token(
    token_id: int,
    token: str,
    alias: str,
    *,
    days: int = 90,
    kinds: list[str] = ["orders", "reviews"],
) -> None:
    since = _ensure_aware_utc(_utc_now() - timedelta(days=days))
    until = _ensure_aware_utc(_utc_now())

    print(f"\n[backfill] {alias}: период {since.isoformat()} .. {until.isoformat()}")

    if "orders" in kinds:
        print(f"[backfill] {alias}: заказы…")
        total = 0
        async for batch in fetch_orders_range(token=token, since=since, until=until, page_size=1000):
            if batch:
                await upsert_orders(batch)
                total += len(batch)
                print(f"[backfill] {alias}: +{len(batch)} заказов (всего={total})")
        print(f"[backfill] {alias}: заказы готово ({total})")

    if "reviews" in kinds:
        print(f"[backfill] {alias}: отзывы…")
        total = 0
        async for batch in fetch_reviews_range(token=token, since=since, until=until, page_size=1000):
            if batch:
                await upsert_reviews(batch)
                total += len(batch)
                print(f"[backfill] {alias}: +{len(batch)} отзывов (всего={total})")
        print(f"[backfill] {alias}: отзывы готово ({total})")


# -------- interactive CLI --------
async def main() -> None:
    tokens: List[Tuple[int, str, str]] = await get_active_tokens()
    if not tokens:
        print("❌ Нет активных токенов (wb_tokens пуста и fallback из .env тоже не задан).")
        return

    print("\n=== Выгрузка данных Wildberries ===")
    print("Доступные магазины:")
    for i, (_, _, alias) in enumerate(tokens, start=1):
        print(f"  {i}. {alias}")

    # выбор магазина
    while True:
        sel = input("\nВыберите магазин (номер) или 0 для всех: ").strip()
        if sel == "0":
            selected = tokens
            break
        if sel.isdigit() and 1 <= int(sel) <= len(tokens):
            selected = [tokens[int(sel) - 1]]
            break
        print("Неверный ввод, попробуйте снова.")

    # выбор типа данных
    print("\nЧто выгружать:")
    print("  1. Только заказы")
    print("  2. Только отзывы")
    print("  3. Всё сразу")
    while True:
        kind_sel = input("Введите номер: ").strip()
        if kind_sel in {"1", "2", "3"}:
            if kind_sel == "1":
                kinds = ["orders"]
            elif kind_sel == "2":
                kinds = ["reviews"]
            else:
                kinds = ["orders", "reviews"]
            break
        print("Неверный ввод, попробуйте снова.")

    # выбор периода
    while True:
        days_inp = input("\nЗа сколько дней выгрузить (по умолчанию 90): ").strip()
        if not days_inp:
            days = 90
            break
        if days_inp.isdigit() and int(days_inp) > 0:
            days = int(days_inp)
            break
        print("Введите положительное число.")

    print("\nЗапуск выгрузки...\n")

    for token_id, token, alias in selected:
        print(f"▶️ {alias}")
        try:
            await backfill_token(token_id, token, alias, days=days, kinds=kinds)
            print(f"✅ {alias} готово\n")
        except Exception as e:
            print(f"❌ {alias} ошибка: {e}\n")

    print("🎯 Все выгрузки завершены.")


if __name__ == "__main__":
    asyncio.run(main())

