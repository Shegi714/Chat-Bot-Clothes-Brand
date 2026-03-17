# models.py
from __future__ import annotations

from typing import Optional, List
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import String, Integer, Boolean, DateTime, Text, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wbshop_bot.storage.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    srid: Mapped[str] = mapped_column(String(64), index=True)

    # новые поля (берём из /supplier/orders)
    sticker: Mapped[Optional[str]] = mapped_column(String(64), index=True)           # sticker
    product_nm_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)     # nmId
    supplier_article: Mapped[Optional[str]] = mapped_column(String(75), index=True)  # supplierArticle
    tech_size: Mapped[Optional[str]] = mapped_column(String(30), index=True)         # techSize

    is_cancel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)

    order_ext_id: Mapped[Optional[str]] = mapped_column(String(128))

    # 💰 стоимость заказа в рублях из finishedPrice (без деления)
    amount_rub: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    reviews: Mapped[List["Review"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan"
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # связь
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        index=True
    )
    order: Mapped[Optional["Order"]] = relationship(back_populates="reviews")

    # из feedbacks API
    review_ext_id: Mapped[str] = mapped_column(String(64), index=True, unique=True)  # id
    text: Mapped[Optional[str]] = mapped_column(Text)
    pros: Mapped[Optional[str]] = mapped_column(Text)
    cons: Mapped[Optional[str]] = mapped_column(Text)
    rating: Mapped[Optional[int]] = mapped_column(Integer)                            # productValuation
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)  # createdDate

    # поля для матчинга с заказом
    last_order_shk_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)  # lastOrderShkId
    last_order_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    nm_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    supplier_article: Mapped[Optional[str]] = mapped_column(String(75), index=True)
    matching_size: Mapped[Optional[str]] = mapped_column(String(30))                  # matchingSize
    user_name: Mapped[Optional[str]] = mapped_column(String(128))

    state: Mapped[Optional[str]] = mapped_column(String(32))  # none / wbRu
    was_viewed: Mapped[Optional[bool]] = mapped_column(Boolean)

    created_row_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncCursor(Base):
    __tablename__ = "sync_cursors"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # храним строку (ISO/ts)


# models.py (добавьте этот класс рядом с остальными моделями)
from sqlalchemy import String, Integer, Boolean, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

class BonusClaim(Base):
    __tablename__ = "bonus_claims"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # фиксируем SRID заказа (делаем уникальным → один чек = один кэшбек)
    srid: Mapped[str] = mapped_column(String(64), index=True, unique=True)

    # ссылка на заказ (на случай расследований), не обязательно всегда проставлена
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # опционально полезно хранить «кто оформил»
    tg_user_id: Mapped[Optional[str]] = mapped_column(String(64))
    tg_username: Mapped[Optional[str]] = mapped_column(String(128))
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    bank: Mapped[Optional[str]] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserDiscount(Base):
    __tablename__ = "user_discounts"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comment: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
