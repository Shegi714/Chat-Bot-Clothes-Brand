# wb_api.py
from __future__ import annotations
import os
import json
import asyncio
import socket
from typing import List, Tuple, Optional, AsyncIterator, Dict, Any
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import text

from db import engine
from secrets_util import dec

# =======================
# Мульти-токены (БД + fallback из .env)
# =======================
async def get_active_tokens() -> List[Tuple[int, str, str]]:
    """
    Возвращает список (token_id, token_plain, alias).
    Сначала читаем из БД wb_tokens.active=1, иначе fallback из .env:
      - WB_API_TOKEN (один токен)
      - WB_API_TOKENS_JSON: [{"alias":"shop-1","token":"..."}, ...]
    """
    out: List[Tuple[int, str, str]] = []
    try:
        async with engine.begin() as conn:
            res = await conn.execute(text("SELECT id, alias, token_enc FROM wb_tokens WHERE active=1"))
            rows = res.mappings().all()
            for r in rows:
                out.append((r["id"], dec(r["token_enc"]), r["alias"]))
        if out:
            return out
    except Exception:
        # если таблицы нет — упадём в fallback
        pass

    env1 = os.getenv("WB_API_TOKEN")
    env_json = os.getenv("WB_API_TOKENS_JSON")
    if env_json:
        try:
            arr = json.loads(env_json)
            for i, it in enumerate(arr, 1):
                out.append((i, it["token"], it.get("alias") or f"env-{i}"))
            return out
        except Exception:
            pass
    if env1:
        out.append((1, env1, "env-main"))
    return out

# =======================
# Курсоры per token
# =======================
async def get_cursor(token_id: int, kind: str) -> Optional[str]:
    async with engine.begin() as conn:
        r = await conn.execute(text(
            "SELECT cursor FROM wb_cursors WHERE token_id=:tid AND kind=:k"
        ), dict(tid=token_id, k=kind))
        m = r.mappings().first()
        return m["cursor"] if m else None

async def set_cursor(token_id: int, kind: str, cursor: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO wb_cursors (token_id, kind, cursor, updated_at)
            VALUES (:tid,:k,:c,CURRENT_TIMESTAMP)
            ON CONFLICT(token_id,kind) DO UPDATE SET cursor=excluded.cursor, updated_at=CURRENT_TIMESTAMP
        """), dict(tid=token_id, k=kind, c=cursor))

# =======================
# Заказы — statistics-api.wildberries.ru
# =======================
WB_STAT_BASE = "https://statistics-api.wildberries.ru"
ORDERS_PATH = "/api/v1/supplier/orders"  # dateFrom=ISO8601; лимит 1 req/min

RATE_LIMIT_SECONDS = 65  # запас к 1 req/min

def _to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _client() -> httpx.AsyncClient:
    # общий клиент; DNS кэшируем немного
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=30.0, connect=15.0))

async def fetch_orders_since(token: str, date_from_iso: str) -> List[Dict[str, Any]]:
    """
    Единичный вызов statistics-api: возвращает массив заказов с dateFrom.
    Заголовок Authorization = <token>.
    """
    url = WB_STAT_BASE + ORDERS_PATH
    headers = {"Authorization": token, "Accept": "application/json"}
    params = {"dateFrom": date_from_iso}
    async with _client() as cli:
        try:
            r = await cli.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            # бывают варианты, где обёртка { "data": [...] }
            return data.get("data") or data.get("orders") or []
        except (httpx.HTTPError, json.JSONDecodeError, socket.gaierror):
            return []

async def fetch_orders_page(token: str, cursor: Optional[str]) -> Dict[str, Any]:
    """
    Совместимо с agent_orders.process_orders_for_token:
    Возвращает {items, has_more, next_cursor}.
    Здесь страничности нет — мы забираем «пачку» за период начиная с cursor (dateFrom),
    обновляем cursor по последнему lastChangeDate и делаем has_more=False (цикл завершится).
    Повторные запуски агента продолжат с нового курсора.
    """
    # Если курсора нет — стартуем за последние 24 часа
    start_dt = datetime.now(timezone.utc) - timedelta(days=1)
    if cursor:
        try:
            start_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        except Exception:
            pass
    date_from_iso = _to_iso_utc(start_dt)

    items = await fetch_orders_since(token, date_from_iso)

    # курсор → lastChangeDate последнего элемента (если есть), иначе прежний
    next_cursor = None
    if items:
        last = items[-1].get("lastChangeDate") or date_from_iso
        # нормализуем к ISO + Z
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except Exception:
            last_dt = start_dt
        next_cursor = _to_iso_utc(last_dt)

    # статистика-апи без пагинации → has_more=False
    return {"items": items, "has_more": False, "next_cursor": next_cursor}

async def fetch_orders_range(token: str, since: datetime, until: datetime, page_size: int = 1000) -> AsyncIterator[List[Dict[str, Any]]]:
    """
    Бэкофил за период: двигаем курсор вперёд по lastChangeDate, соблюдая лимит 1 req/min.
    Возвращаем батчи, чтобы upsert шёл порциями.
    """
    current = since
    while True:
        batch = await fetch_orders_since(token, _to_iso_utc(current))
        if not batch:
            break
        yield batch
        # обновляем курсор — lastChangeDate последнего
        last_str = batch[-1].get("lastChangeDate")
        if not last_str:
            break
        try:
            current = datetime.fromisoformat(str(last_str).replace("Z", "+00:00"))
        except Exception:
            break
        if current >= until:
            break
        await asyncio.sleep(RATE_LIMIT_SECONDS)

# =======================
# Отзывы — feedbacks-api.wildberries.ru
# =======================
WB_FEEDBACKS_URL = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks"

async def fetch_reviews_page(token: str, cursor: Optional[str]) -> Dict[str, Any]:
    """
    Совместимо с текущим agent_reviews: используем параметры, как в логах:
      isAnswered=<bool>, take=5000, skip=0, order=dateAsc, dateFrom=<unix>, dateTo=<unix>
    Здесь cursor трактуем как unix timestamp «from». Если не задан — возьмём за последние сутки.
    За один вызов сделаем два прохода: isAnswered=false и isAnswered=true и склеим.
    next_cursor вернём как dateTo (чтобы агент мог сдвинуться при желании).
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    from_ts = now_ts - 24 * 3600
    if cursor:
        try:
            # если пришёл ISO — переведём в ts; если уже ts — оставим
            if cursor.isdigit():
                from_ts = int(cursor)
            else:
                from_ts = int(datetime.fromisoformat(cursor.replace("Z", "+00:00")).timestamp())
        except Exception:
            pass

    headers = {"Authorization": token, "Accept": "application/json"}
    params_base = {
        "take": 5000,
        "skip": 0,
        "order": "dateAsc",
        "dateFrom": from_ts,
        "dateTo": now_ts,
    }

    items_all: List[Dict[str, Any]] = []
    async with _client() as cli:
        for answered in (False, True):
            params = dict(params_base)
            params["isAnswered"] = "true" if answered else "false"
            try:
                r = await cli.get(WB_FEEDBACKS_URL, headers=headers, params=params)
                r.raise_for_status()
                data = r.json()
                # в ответе WB обычно {"data":{"feedbacks":[...]}} или {"feedbacks":[...]}
                items = []
                if isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], dict):
                        items = data["data"].get("feedbacks") or []
                    else:
                        items = data.get("feedbacks") or data.get("items") or []
                elif isinstance(data, list):
                    items = data
                if items:
                    items_all.extend(items)
            except (httpx.HTTPError, json.JSONDecodeError, socket.gaierror):
                # пропускаем этот проход
                continue

    # хинт агенту: курсор можно передвинуть вперёд на текущий to
    return {"items": items_all, "has_more": False, "next_cursor": str(now_ts)}

async def fetch_reviews_range(token: str, since: datetime, until: datetime, page_size: int = 1000) -> AsyncIterator[List[Dict[str, Any]]]:
    """
    Бэкофил отзывов за период: идём кусками по суткам (или как удобнее),
    чтобы не ловить слишком большие ответы.
    """
    cur = since
    step = timedelta(days=1)
    while cur < until:
        nxt = min(cur + step, until)
        headers = {"Authorization": token, "Accept": "application/json"}
        params = {
            "take": 5000,
            "skip": 0,
            "order": "dateAsc",
            "dateFrom": int(cur.timestamp()),
            "dateTo": int(nxt.timestamp()),
        }
        batch: List[Dict[str, Any]] = []
        async with _client() as cli:
            for answered in (False, True):
                p = dict(params)
                p["isAnswered"] = "true" if answered else "false"
                try:
                    r = await cli.get(WB_FEEDBACKS_URL, headers=headers, params=p)
                    r.raise_for_status()
                    data = r.json()
                    if isinstance(data, dict):
                        if "data" in data and isinstance(data["data"], dict):
                            items = data["data"].get("feedbacks") or []
                        else:
                            items = data.get("feedbacks") or data.get("items") or []
                    elif isinstance(data, list):
                        items = data
                    else:
                        items = []
                    if items:
                        batch.extend(items)
                except (httpx.HTTPError, json.JSONDecodeError, socket.gaierror):
                    continue
        if batch:
            yield batch
        cur = nxt
