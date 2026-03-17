# ui/notify.py
from __future__ import annotations
import asyncio
import re
from typing import Iterable, List, Optional, Set, Dict, Any, Tuple

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import text
from db import engine

# 👇 добавлено: чтобы уметь вернуть пользователя в Главное меню
from ui.menu import main_menu_inline, send_main_menu_inline

from config import NOTIFY_SOURCE_CHANNEL

router = Router(name="notify")

# =========================
# НАСТРОЙКИ
# =========================

# Ссылка/username/ID канала-источника. Примеры:
# "https://t.me/example_brand_news", "@example_brand_news", "example_brand_news", -1001234567890
SOURCE_CHANNEL = NOTIFY_SOURCE_CHANNEL  # настраивается через env

# Хештеги, на которые реагируем (без #). Любой из списка → пересылка.
TRACK_TAGS: List[str] = [
    "Розыгрыш", "Конкурс", "Приз", "Бесплатно", "Подарок", "Новинка"
]
# нормализованный набор в нижнем регистре, без решётки
TRACK_TAGS_LOWER: Set[str] = {t.lower().lstrip("#") for t in TRACK_TAGS}

# Текст экрана «персональные уведомления»
NOTIFY_TEXT = (
    "Получай уведомления о новинках, капсулах и розыгрышах первым 💌\n"
    "Мы не шлём спам — только важное и вдохновляющее."
)

# ===== ALBUM SETTINGS =====
# задержка, чтобы «досыпались» все части альбома прежде чем слать подписчикам
ALBUM_FLUSH_DELAY_SEC = 1.2

# =========================
# ИНИЦИАЛИЗАЦИЯ ХРАНИЛИЩА
# =========================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notify_subs (
    user_id     BIGINT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_PROCESSED_MESSAGES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notify_processed_messages (
    message_id     BIGINT PRIMARY KEY,
    chat_id        BIGINT NOT NULL,
    media_group_id TEXT,
    processed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_PROCESSED_MESSAGES_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_notify_processed_media_group 
    ON notify_processed_messages(media_group_id) WHERE media_group_id IS NOT NULL;
"""

async def init_notify_storage() -> None:
    async with engine.begin() as conn:
        await conn.execute(text(CREATE_TABLE_SQL))
        await conn.execute(text(CREATE_PROCESSED_MESSAGES_TABLE_SQL))
        await conn.execute(text(CREATE_PROCESSED_MESSAGES_INDEX_SQL))

# =========================
# DAO-утилиты
# =========================

async def set_subscription(user_id: int, enabled: bool) -> None:
    async with engine.begin() as conn:
        # Используем INSERT ... ON CONFLICT для создания или обновления записи
        # Это работает и в SQLite, и в PostgreSQL
        await conn.execute(
            text("""
            INSERT INTO notify_subs (user_id, enabled)
            VALUES (:uid, :en)
            ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled
            """),
            {"uid": user_id, "en": 1 if enabled else 0}
        )

async def is_enabled(user_id: int) -> bool:
    async with engine.begin() as conn:
        r = await conn.execute(text("SELECT enabled FROM notify_subs WHERE user_id=:uid"), {"uid": user_id})
        m = r.mappings().first()
        return bool(m and (m["enabled"] == 1))

async def get_all_enabled_user_ids() -> List[int]:
    async with engine.begin() as conn:
        r = await conn.execute(text("SELECT user_id FROM notify_subs WHERE enabled=1"))
        return [int(row[0]) for row in r.fetchall()]

async def is_message_processed(message_id: int, media_group_id: Optional[str] = None) -> bool:
    """Проверяет, было ли сообщение уже обработано."""
    async with engine.begin() as conn:
        if media_group_id:
            # Для альбомов проверяем по media_group_id
            r = await conn.execute(
                text("SELECT 1 FROM notify_processed_messages WHERE media_group_id = :mgid LIMIT 1"),
                {"mgid": str(media_group_id)}
            )
        else:
            # Для обычных сообщений проверяем по message_id
            r = await conn.execute(
                text("SELECT 1 FROM notify_processed_messages WHERE message_id = :mid LIMIT 1"),
                {"mid": message_id}
            )
        return r.first() is not None

async def mark_message_processed(message_id: int, chat_id: int, media_group_id: Optional[str] = None, all_message_ids: Optional[List[int]] = None) -> None:
    """Помечает сообщение(я) как обработанное(ые)."""
    async with engine.begin() as conn:
        if all_message_ids:
            # Для альбомов сохраняем все message_id
            for mid in all_message_ids:
                await conn.execute(
                    text("""
                    INSERT INTO notify_processed_messages (message_id, chat_id, media_group_id)
                    VALUES (:mid, :cid, :mgid)
                    ON CONFLICT(message_id) DO NOTHING
                    """),
                    {"mid": mid, "cid": chat_id, "mgid": str(media_group_id) if media_group_id else None}
                )
        else:
            # Для обычных сообщений сохраняем только один message_id
            await conn.execute(
                text("""
                INSERT INTO notify_processed_messages (message_id, chat_id, media_group_id)
                VALUES (:mid, :cid, :mgid)
                ON CONFLICT(message_id) DO NOTHING
                """),
                {"mid": message_id, "cid": chat_id, "mgid": str(media_group_id) if media_group_id else None}
            )

# =========================
# Клавиатуры
# =========================

def kb_notify_menu(enabled_now: Optional[bool] = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Включить уведомления", callback_data="notify:on")
    kb.button(text="🔕 Отключить уведомления", callback_data="notify:off")
    kb.button(text="⬅ Назад", callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()

# =========================
# Вспомогательные функции
# =========================

def _normalize_source(source: str) -> str:
    s = source.strip()
    if s.startswith("https://t.me/"):
        s = s.rsplit("/", 1)[-1]
    if s.startswith("@"):
        s = s[1:]
    return s

# ---- ХЕШТЕГИ: строгий матч только по символу '#' ----

_HASHTAG_RE = re.compile(r"#([A-Za-zА-Яа-яЁё0-9_]+)", flags=re.UNICODE)

def _extract_hashtags_raw(text: str) -> Set[str]:
    """
    Возвращает набор хэштегов БЕЗ # в нижнем регистре.
    Пример: 'Тут #Розыгрыш и #новинка_2025' -> {'розыгрыш', 'новинка_2025'}
    """
    if not text:
        return set()
    return {t.lower() for t in _HASHTAG_RE.findall(text)}

def _has_tracked_hashtag(text: str) -> bool:
    """
    Истинно только если среди ЯВНЫХ хэштегов встретился один из TRACK_TAGS.
    Обычное слово 'розыгрыш' без '#': НЕ триггерит.
    """
    if not TRACK_TAGS_LOWER:
        return False
    found = _extract_hashtags_raw(text)
    return any(tag in TRACK_TAGS_LOWER for tag in found)

def _match_tags(msg: Message) -> bool:
    text = (msg.text or "") + "\n" + (msg.caption or "")
    return _has_tracked_hashtag(text)

async def _resolve_source_chat_id(bot, source: str) -> Optional[int]:
    try:
        if not hasattr(bot, "_notify_source_cache"):
            bot._notify_source_cache = {}
        cache = bot._notify_source_cache
        if source in cache:
            return cache[source]
        norm = _normalize_source(source)
        chat = await bot.get_chat(norm if norm.lstrip("-").isdigit() is False else int(norm))
        cache[source] = chat.id
        return chat.id
    except Exception:
        return None

# ===== ALBUM BUFFER (in-memory) =====

def _album_state(bot) -> Dict[str, Any]:
    """
    Вешаем служебное состояние на объект бота, чтобы не плодить глобалы.
    """
    if not hasattr(bot, "_notify_album_state"):
        bot._notify_album_state = {
            "buffers": {},   # media_group_id -> List[Message]
            "timers": {},    # media_group_id -> asyncio.Task
            "lock": asyncio.Lock(),
        }
    return bot._notify_album_state

async def _album_enqueue_and_schedule(msg: Message) -> None:
    state = _album_state(msg.bot)
    mgid = str(msg.media_group_id)
    async with state["lock"]:
        buf: Dict[str, List[Message]] = state["buffers"]
        timers: Dict[str, asyncio.Task] = state["timers"]
        buf.setdefault(mgid, []).append(msg)
        # сортировка по message_id для сохранения порядка
        buf[mgid].sort(key=lambda m: m.message_id)

        # если таймер уже есть — не плодим новый
        if mgid not in timers:
            timers[mgid] = asyncio.create_task(_album_flush_after_delay(msg.bot, mgid))

async def _album_flush_after_delay(bot, mgid: str) -> None:
    try:
        await asyncio.sleep(ALBUM_FLUSH_DELAY_SEC)
        await _album_flush(bot, mgid)
    finally:
        # уборка таймера
        state = _album_state(bot)
        async with state["lock"]:
            state["timers"].pop(mgid, None)

async def _album_flush(bot, mgid: str) -> None:
    state = _album_state(bot)
    async with state["lock"]:
        parts: List[Message] = state["buffers"].pop(mgid, [])

    if not parts:
        return

    # Проверяем, не был ли этот альбом уже обработан
    if await is_message_processed(parts[0].message_id, mgid):
        return

    # текст для матчей берём из первой части, где есть caption/text
    caption_source = next((p for p in parts if (p.caption or p.text)), parts[0])
    caption_text = (caption_source.caption or caption_source.text or "").strip()

    # если хэштегов нет — ничего не рассылаем
    if not _has_tracked_hashtag(caption_text):
        return

    # собираем медиагруппу (фото/видео)
    media = []
    for idx, p in enumerate(parts):
        if p.photo:
            file_id = p.photo[-1].file_id  # самое большое превью
            if idx == 0 and caption_text:
                media.append(InputMediaPhoto(media=file_id, caption=caption_text))
            else:
                media.append(InputMediaPhoto(media=file_id))
        elif p.video:
            file_id = p.video.file_id
            if idx == 0 and caption_text:
                media.append(InputMediaVideo(media=file_id, caption=caption_text))
            else:
                media.append(InputMediaVideo(media=file_id))
        # можно расширить и на document/audio/animation при необходимости

    if not media:
        # если нечего группировать — fallback на первую часть как обычную копию
        try:
            user_ids = await get_all_enabled_user_ids()
            if user_ids:
                for uid in user_ids:
                    try:
                        await bot.copy_message(chat_id=uid, from_chat_id=parts[0].chat.id, message_id=parts[0].message_id)
                        await asyncio.sleep(0.03)
                    except Exception:
                        continue
                # Помечаем альбом как обработанный после успешной рассылки
                await mark_message_processed(parts[0].message_id, parts[0].chat.id, mgid)
        except Exception:
            pass
        return

    # рассылаем медиагруппой всем подписчикам
    try:
        user_ids = await get_all_enabled_user_ids()
        if user_ids:
            for uid in user_ids:
                try:
                    await bot.send_media_group(chat_id=uid, media=media)
                    await asyncio.sleep(0.05)
                except Exception:
                    continue
            # Помечаем альбом как обработанный после успешной рассылки (сохраняем все message_id)
            all_ids = [p.message_id for p in parts]
            await mark_message_processed(parts[0].message_id, parts[0].chat.id, mgid, all_ids)
    except Exception:
        # не роняем обработчик; просто проглатываем
        return

# =========================
# Роутинг: экран меню
# =========================

@router.callback_query(F.data == "menu:notify")
async def open_notify_menu(call: CallbackQuery):
    try:
        cur_enabled = await is_enabled(call.from_user.id)
    except Exception:
        cur_enabled = None
    text = NOTIFY_TEXT + (f"\n\nТекущий статус: {'включены' if cur_enabled else 'выключены'}." if cur_enabled is not None else "")
    try:
        await call.message.edit_text(text, reply_markup=kb_notify_menu(cur_enabled))
    except Exception:
        await call.message.answer(text, reply_markup=kb_notify_menu(cur_enabled))
    await call.answer()

@router.callback_query(F.data == "notify:on")
async def enable_notify(call: CallbackQuery):
    try:
        await set_subscription(call.from_user.id, True)
        # 👉 сразу возвращаем в «Главное меню»
        try:
            await call.message.edit_text("Главное меню:", reply_markup=main_menu_inline())
            await call.answer("Уведомления включены")
        except Exception as e1:
            try:
                await send_main_menu_inline(call.message)
                await call.answer("Уведомления включены")
            except Exception as e2:
                await call.answer(f"Уведомления включены, но не удалось обновить меню: {str(e2)}", show_alert=True)
    except Exception as e:
        await call.answer(f"Ошибка при включении уведомлений: {str(e)}", show_alert=True)

@router.callback_query(F.data == "notify:off")
async def disable_notify(call: CallbackQuery):
    try:
        await set_subscription(call.from_user.id, False)
        # 👉 сразу возвращаем в «Главное меню»
        try:
            await call.message.edit_text("Главное меню:", reply_markup=main_menu_inline())
            await call.answer("Уведомления выключены")
        except Exception as e1:
            try:
                await send_main_menu_inline(call.message)
                await call.answer("Уведомления выключены")
            except Exception as e2:
                await call.answer(f"Уведомления выключены, но не удалось обновить меню: {str(e2)}", show_alert=True)
    except Exception as e:
        await call.answer(f"Ошибка при отключении уведомлений: {str(e)}", show_alert=True)

# =========================
# Пересылка из канала-источника подписчикам
# =========================

async def _should_process(msg: Message) -> bool:
    src_id = await _resolve_source_chat_id(msg.bot, SOURCE_CHANNEL)
    if not src_id or msg.chat.id != src_id:
        return False
    return True

@router.channel_post()   # новые посты
async def on_channel_post(msg: Message, is_edited: bool = False):
    if not await _should_process(msg):
        return

    # ===== обработка альбомов =====
    if msg.media_group_id:
        # Для отредактированных альбомов проверяем, не был ли уже обработан
        if is_edited and await is_message_processed(msg.message_id, str(msg.media_group_id)):
            return
        # буферизуем и рассылаем медиагруппой после небольшой задержки
        await _album_enqueue_and_schedule(msg)
        return

    # Проверяем, не было ли сообщение уже обработано (для отредактированных)
    if is_edited and await is_message_processed(msg.message_id):
        return

    # обычное сообщение (фото/текст/видео единичное)
    if not _match_tags(msg):
        return
    user_ids = await get_all_enabled_user_ids()
    if not user_ids:
        return
    for uid in user_ids:
        try:
            await msg.bot.copy_message(chat_id=uid, from_chat_id=msg.chat.id, message_id=msg.message_id)
            await asyncio.sleep(0.03)
        except Exception:
            continue
    # Помечаем сообщение как обработанное после успешной рассылки
    await mark_message_processed(msg.message_id, msg.chat.id)

@router.edited_channel_post()
async def on_channel_post_edited(msg: Message):
    # Для отредактированных сообщений проверяем, не было ли уже обработано
    # Это предотвращает повторную рассылку при периодических обновлениях от других ботов
    await on_channel_post(msg, is_edited=True)
