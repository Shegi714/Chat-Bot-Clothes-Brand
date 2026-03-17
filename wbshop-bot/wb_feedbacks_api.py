# wb_feedbacks_api.py
import os
from typing import List, Dict, Optional
import time
import httpx

BASE = "https://feedbacks-api.wildberries.ru"

def _headers():
    key = os.getenv("WB_API_KEY", "")
    return {"Authorization": key, "Accept": "application/json"}

RATE_LIMIT_DELAY = 0.35  # 3 req/sec (берём запас)

async def fetch_feedbacks(*, is_answered: bool, take: int, skip: int,
                          order: str = "dateAsc",
                          date_from_ts: Optional[int] = None,
                          date_to_ts: Optional[int] = None) -> Dict:
    """
    GET /api/v1/feedbacks
    Возвращает объект с полями: data: { feedbacks: [...], countUnanswered, countArchive }
    """
    params = {
        "isAnswered": str(is_answered).lower(),
        "take": take,
        "skip": skip,
        "order": order,
    }
    if date_from_ts is not None:
        params["dateFrom"] = date_from_ts
    if date_to_ts is not None:
        params["dateTo"] = date_to_ts

    url = f"{BASE}/api/v1/feedbacks"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, params=params, headers=_headers())
        r.raise_for_status()
        data = r.json()
    await _sleep_rate_limit()
    return data

async def _sleep_rate_limit():
    import asyncio
    await asyncio.sleep(RATE_LIMIT_DELAY)
