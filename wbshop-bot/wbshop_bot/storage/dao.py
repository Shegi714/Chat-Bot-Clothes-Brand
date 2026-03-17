# dao.py
from typing import Sequence, Optional
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from wbshop_bot.storage.models import Order, Review, SyncCursor, BonusClaim, UserDiscount
from wbshop_bot.storage.db import async_session_maker  # добавили для пакетных upsert'ов

CURSOR_KEY_ORDERS = "wb_orders_last_change"
CURSOR_KEY_FEEDBACKS = "wb_feedbacks_last_ts"   # Unix timestamp (int) в строке

# ---------- cursor helpers ----------
async def get_cursor(session: AsyncSession, key: str) -> str | None:
    q = await session.execute(select(SyncCursor).where(SyncCursor.key == key))
    row = q.scalar_one_or_none()
    return row.value if row else None

async def set_cursor(session: AsyncSession, key: str, value: str) -> None:
    q = await session.execute(select(SyncCursor).where(SyncCursor.key == key))
    row = q.scalar_one_or_none()
    if row:
        row.value = value
    else:
        session.add(SyncCursor(key=key, value=value))
    await session.commit()

# ---------- util ----------
def _to_aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _parse_iso_tz(val: str | None, assume_tz: timezone | None = timezone.utc) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz or timezone.utc)
    return dt.astimezone(timezone.utc)

def _to_money(v) -> Decimal | None:
    """
    Нормализует денежное значение в Decimal(2). Принимаем только > 0.
    Используем для finishedPrice (РУБЛИ, без деления).
    """
    if v is None:
        return None
    try:
        d = Decimal(str(v))
        if d <= 0:
            return None
        return d.quantize(Decimal("0.01"))
    except Exception:
        return None

# ---------- SRID helpers ----------
def _srid_normalize(s: str | None) -> str | None:
    """
    Нормализация SRID:
    - всё, что начинается на 'd' (любой кейс), переводим в нижний регистр целиком: d*, du/db/dc, dT., d2. и т.п.;
    - остальное (включая чисто числовые) оставляем как есть.
    """
    if not s:
        return s
    s = s.strip()
    low = s.lower()
    if low.startswith("d"):
        return low
    return s

def srid_core(s: str | None) -> str:
    """
    Базовая часть SRID для матчинга:
      - для форм 'dX.<hex>.<x>.<y>' / 'd2.<hex>.<x>.<y>' / 'du.<hex>...' → ядро 'dX.<hex>' (две первые части);
      - для форм 'dc/db/du' это тоже работает;
      - для чисто цифровых '123456...(.x.y)' → ядро '123456...';
      - для прочего → первая часть до точки.
    """
    if not s:
        return ""
    s = _srid_normalize(s) or ""
    parts = s.split(".")
    if not parts:
        return ""

    p0 = parts[0]
    if p0.startswith("d"):
        if len(parts) >= 2:
            return f"{p0}.{parts[1]}"
        return p0
    return parts[0]

# ---------- orders ----------
async def upsert_order_from_wb(session: AsyncSession, wb: dict) -> Order:
    srid = wb.get("srid")
    if not srid:
        raise ValueError("WB order has no srid")
    srid = _srid_normalize(str(srid))

    is_cancel = bool(wb.get("isCancel", False))
    order_date = _parse_iso_tz(wb.get("date"), assume_tz=timezone(timedelta(hours=3)))  # МСК → UTC
    nm_id = str(wb.get("nmId")) if wb.get("nmId") is not None else None
    sticker = str(wb.get("sticker")) if wb.get("sticker") is not None else None  # может прийти числом
    supplier_article = wb.get("supplierArticle") or None
    tech_size = wb.get("techSize") or None

    # 💰 стоимость заказа из finishedPrice (РУБЛИ, без деления)
    amount_rub = _to_money(wb.get("finishedPrice"))

    q = await session.execute(select(Order).where(Order.srid == srid))
    obj = q.scalar_one_or_none()
    if obj:
        obj.is_cancel = is_cancel
        obj.date = order_date
        if nm_id:
            obj.product_nm_id = nm_id
        if sticker is not None:
            obj.sticker = sticker
        if supplier_article:
            obj.supplier_article = supplier_article
        if tech_size:
            obj.tech_size = tech_size
        if amount_rub is not None:
            obj.amount_rub = amount_rub
    else:
        obj = Order(
            srid=srid,
            is_cancel=is_cancel,
            date=order_date,
            product_nm_id=nm_id,
            sticker=sticker,
            supplier_article=supplier_article,
            tech_size=tech_size,
            amount_rub=amount_rub,  # может быть None — ОК
        )
        session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj

async def find_orders_by_srids_fuzzy(session: AsyncSession, srids: Sequence[str]) -> list[Order]:
    """
    Ищем заказы по входным SRID:
      - точное совпадение (нормализация dc/du/db.* → lower);
      - по "ядру": '<core>.%' (цифровой → '<digits>.%', dc/du/db → '<prefix>.<hex>.%').
    """
    if not srids:
        return []
    srids_clean = [_srid_normalize(s.strip()) for s in srids if s and s.strip()]
    if not srids_clean:
        return []
    cores = list({ srid_core(s) for s in srids_clean if s })

    conds = [Order.srid.in_(srids_clean)]
    for core in cores:
        if core:
            conds.append(Order.srid.ilike(core + ".%"))

    q = await session.execute(select(Order).where(or_(*conds)))
    return list(q.scalars().all())

# ---------- reviews ----------
async def upsert_review_from_wb(session: AsyncSession, fb: dict) -> Review:
    """
    Апсерт отзыва. Привязка к заказу — ТОЛЬКО:
      lastOrderShkId (отзыв) == orders.sticker (заказ).
    Никаких фолбэков по nmId/датам/размерам.
    Также устраняем дубликаты review_ext_id на лету.
    """
    rid = str(fb.get("id"))
    if not rid:
        raise ValueError("Feedback without id")

    # базовые поля
    text = fb.get("text") or None
    pros = fb.get("pros") or None
    cons = fb.get("cons") or None
    rating = fb.get("productValuation")
    created = _parse_iso_tz(fb.get("createdDate"), assume_tz=timezone(timedelta(hours=3)))
    state = fb.get("state") or None
    was_viewed = bool(fb.get("wasViewed")) if fb.get("wasViewed") is not None else None
    user_name = fb.get("userName") or None

    # productDetails — сохраняем для информации, НО НЕ используем для связи
    product = fb.get("productDetails") or {}
    nm_id = str(product.get("nmId")) if product.get("nmId") is not None else None
    supplier_article = product.get("supplierArticle") or None
    size = product.get("size") or product.get("techSize") or None

    last_shk = str(fb.get("lastOrderShkId")) if fb.get("lastOrderShkId") is not None else None
    last_created = _parse_iso_tz(fb.get("lastOrderCreatedAt"), assume_tz=timezone(timedelta(hours=3)))

    # ищем по review_ext_id, но безопасно: убираем дубликаты
    res = await session.execute(select(Review).where(Review.review_ext_id == rid))
    rows = list(res.scalars().all())
    obj = None
    if len(rows) > 1:
        rows.sort(key=lambda r: r.id, reverse=True)
        obj = rows[0]
        for dup in rows[1:]:
            await session.delete(dup)
        await session.flush()
    elif len(rows) == 1:
        obj = rows[0]

    if obj:
        obj.text = text
        obj.pros = pros
        obj.cons = cons
        obj.rating = rating
        obj.created_at = created
        obj.last_order_shk_id = last_shk
        obj.last_order_created_at = last_created
        obj.nm_id = nm_id or obj.nm_id
        obj.supplier_article = supplier_article or obj.supplier_article
        obj.matching_size = size or obj.matching_size
        obj.state = state
        obj.was_viewed = was_viewed
        obj.user_name = user_name or obj.user_name
    else:
        obj = Review(
            review_ext_id=rid,
            text=text, pros=pros, cons=cons, rating=rating,
            created_at=created, state=state, was_viewed=was_viewed, user_name=user_name,
            last_order_shk_id=last_shk, last_order_created_at=last_created,
            nm_id=nm_id, supplier_article=supplier_article, matching_size=size,
        )
        session.add(obj)

    # Привязка к заказу — только по sticker, безопасно обрабатываем дубли
    if obj.order_id is None and obj.last_order_shk_id:
        res_o = await session.execute(select(Order).where(Order.sticker == obj.last_order_shk_id))
        orders = list(res_o.scalars().all())
        if orders:
            if len(orders) > 1:
                orders.sort(
                    key=lambda o: (
                        (o.date or datetime.min.replace(tzinfo=timezone.utc)),
                        o.id
                    ),
                    reverse=True
                )
            primary = orders[0]
            obj.order_id = primary.id

    await session.commit()
    await session.refresh(obj)
    return obj

async def find_reviews_for_orders(session: AsyncSession, order_ids: Sequence[int]) -> list[Review]:
    if not order_ids:
        return []
    q = await session.execute(
        select(Review).where(Review.order_id.in_(list(order_ids))).order_by(Review.created_at.desc())
    )
    return list(q.scalars().all())

async def find_reviews_by_stickers(session: AsyncSession, stickers: Sequence[str]) -> list[Review]:
    """
    Возвращаем отзывы, где lastOrderShkId ∈ stickers.
    Полезно, если связь order_id ещё не успела проставиться, но sticker у заказа есть.
    """
    if not stickers:
        return []
    stickers = [str(s) for s in stickers if s]
    if not stickers:
        return []
    q = await session.execute(
        select(Review)
        .where(Review.last_order_shk_id.in_(stickers))
        .order_by(Review.created_at.desc())
    )
    return list(q.scalars().all())

async def update_order_sticker_if_empty(session: AsyncSession, order_id: int, sticker: str) -> bool:
    """Проставить sticker, если у заказа он ещё пуст. Возвращает True, если обновили."""
    if not sticker:
        return False
    q = await session.execute(select(Order).where(Order.id == order_id))
    o = q.scalar_one_or_none()
    if not o:
        return False
    if o.sticker:
        return False
    o.sticker = str(sticker)
    await session.commit()
    return True

# ---------- bonus claims ----------
async def get_claimed_srids(session: AsyncSession, srids: Sequence[str]) -> set[str]:
    """Возвращает множество SRID, которые уже погашены (есть в bonus_claims)."""
    if not srids:
        return set()
    srids = [s for s in srids if s]
    if not srids:
        return set()
    q = await session.execute(select(BonusClaim.srid).where(BonusClaim.srid.in_(srids)))
    return set(q.scalars().all())

async def insert_claims_for_orders(
    session: AsyncSession,
    srid_to_order_id: dict[str, Optional[int]],
    *,
    tg_user_id: str | None = None,
    tg_username: str | None = None,
    phone: str | None = None,
    bank: str | None = None,
) -> int:
    """
    Вставляет записи в bonus_claims для каждой пары (srid -> order_id), пропуская уже существующие SRID.
    Возвращает количество добавленных записей.
    """
    if not srid_to_order_id:
        return 0

    existing = await get_claimed_srids(session, list(srid_to_order_id.keys()))
    added = 0
    for srid, oid in srid_to_order_id.items():
        if not srid or srid in existing:
            continue
        session.add(BonusClaim(
            srid=srid,
            order_id=oid,
            tg_user_id=str(tg_user_id) if tg_user_id is not None else None,
            tg_username=tg_username,
            phone=phone,
            bank=bank,
        ))
        added += 1
    if added:
        await session.commit()
    return added

# ---------- user discounts ----------
async def set_user_discount(user_id: int, comment: str) -> None:
    """Устанавливает или обновляет скидку/комментарий для пользователя."""
    async with async_session_maker() as session:
        q = await session.execute(select(UserDiscount).where(UserDiscount.user_id == user_id))
        obj = q.scalar_one_or_none()
        if obj:
            obj.comment = comment
        else:
            session.add(UserDiscount(user_id=user_id, comment=comment))
        await session.commit()

async def get_user_discount(user_id: int) -> str | None:
    """Возвращает текст скидки/комментария для пользователя или None."""
    async with async_session_maker() as session:
        q = await session.execute(select(UserDiscount).where(UserDiscount.user_id == user_id))
        obj = q.scalar_one_or_none()
        return obj.comment if obj else None

# ---------- batch upserts for agents (НОВОЕ) ----------
async def upsert_orders(items: Sequence[dict]) -> None:
    """
    Пакетная обработка заказов из агентов.
    Использует существующую upsert_order_from_wb для каждого элемента.
    """
    if not items:
        return
    async with async_session_maker() as session:
        for it in items:
            try:
                await upsert_order_from_wb(session, it)
            except Exception:
                # не рвём пачку из-за одного кривого элемента
                continue

async def upsert_reviews(items: Sequence[dict]) -> None:
    """
    Пакетная обработка отзывов из агентов.
    Использует существующую upsert_review_from_wb для каждого элемента.
    """
    if not items:
        return
    async with async_session_maker() as session:
        for it in items:
            try:
                await upsert_review_from_wb(session, it)
            except Exception:
                continue
