import os
import asyncio
import html
import secrets
from datetime import datetime, timezone
from typing import Optional, Set
from aiogram.exceptions import TelegramBadRequest
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ContentType
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

# Demo/public-safe branding defaults
from config import BRAND_NAME, BRAND_TAG, SUPPORT_GROUP_ID as CFG_SUPPORT_GROUP_ID, GENERAL_THREAD_ID as CFG_GENERAL_THREAD_ID

# === БД-репозиторий тикетов ===
from support_repo import (
    init_tables as support_init_tables,
    insert_ticket, update_ticket,
    get_by_thread, get_by_ticket,
    get_current_for_user,   # умный выбор текущего тикета (переживает рестарты)
)

# БД для скидок
from dao import set_user_discount, get_user_discount

# UI
from ui.menu import ticket_resolved_feedback_inline, send_main_menu_inline

# ==== ENV / CONSTS ====
# NOTE: we still allow overriding via env, but parse once in config.py
SUPPORT_GROUP_ID = int(CFG_SUPPORT_GROUP_ID)   # супергруппа (отрицательный id)
PROJECT_TAG = os.getenv("PROJECT_TAG", BRAND_TAG)
AUTOCLOSE_HOURS = int(os.getenv("AUTOCLOSE_HOURS", "48"))
TICKET_TTL_MINUTES = int(os.getenv("TICKET_TTL_MINUTES", "45"))  # на будущее
GENERAL_THREAD_ID_ENV = os.getenv("GENERAL_THREAD_ID")       # message_thread_id темы General / Statuses (legacy)

router = Router()

# ==== General thread id (берём из .env) ====
_general_thread_id_cache: Optional[int] = int(CFG_GENERAL_THREAD_ID) if int(CFG_GENERAL_THREAD_ID or 0) else (int(GENERAL_THREAD_ID_ENV) if GENERAL_THREAD_ID_ENV else None)
async def get_general_thread_id() -> Optional[int]:
    return _general_thread_id_cache

# ==== Ticket status ====
class TStatus:
    OPEN = "OPEN"
    PENDING_USER = "PENDING_USER"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"

def status_badge(s: str) -> str:
    return {
        TStatus.OPEN: "🟡 OPEN",
        TStatus.PENDING_USER: "🟠 PENDING_USER",
        TStatus.RESOLVED: "🟢 RESOLVED",
        TStatus.CLOSED: "🔒 CLOSED",
    }.get(s, s)

# --- Реестр кэшбекных тикетов
_bonus_ticket_ids: Set[str] = set()
def register_bonus_ticket(ticket_id: str) -> None:
    _bonus_ticket_ids.add(ticket_id)

def unregister_bonus_ticket(ticket_id: str) -> None:
    _bonus_ticket_ids.discard(ticket_id)

def _is_bonus_ticket(ticket_id: str) -> bool:
    try:
        return ticket_id.startswith("BONUS-") or (ticket_id in _bonus_ticket_ids)
    except Exception:
        return False

def ticket_keyboard(ticket_id: str, current: str) -> InlineKeyboardMarkup:
    """
    Базовые кнопки (всегда):
      - ✅ Решено → t:{ticket_id}:set:RESOLVED
      - 📩 Ждём данные → t:{ticket_id}:set:PENDING_USER
      - 🔒 Закрыть → t:{ticket_id}:close

    Дополнительно для кэшбекных тикетов:
      - 🎉 кэшбек OK → bonus:ok:{ticket_id}
      - 🚫 кэшбек не подходит → bonus:no:{ticket_id}
    """
    rows = [
        [
            InlineKeyboardButton(text="✅ Решено", callback_data=f"t:{ticket_id}:set:{TStatus.RESOLVED}"),
            InlineKeyboardButton(text="📩 Ждём данные", callback_data=f"t:{ticket_id}:set:{TStatus.PENDING_USER}"),
        ],
        [
            InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"t:{ticket_id}:close"),
            InlineKeyboardButton(text="✏️ Ред. тему", callback_data=f"t:{ticket_id}:edit_topic"),
        ],
        [
            InlineKeyboardButton(text="🏷 Доп. скидка", callback_data=f"t:{ticket_id}:add_discount"),
        ]
    ]
    if _is_bonus_ticket(ticket_id):
        rows.append([
            InlineKeyboardButton(text="🎉 кэшбек OK", callback_data=f"bonus:ok:{ticket_id}"),
            InlineKeyboardButton(text="🚫 кэшбек не подходит", callback_data=f"bonus:no:{ticket_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ==== ДОБАВЛЕНО: кнопка в главное меню ====
CB_TO_MAIN = "support:to_main"

def kb_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="в главное меню", callback_data=CB_TO_MAIN)]
    ])

# ==== FSM ====
class SupportFSM(StatesGroup):
    wait_one_message = State()

class SupportAdminFSM(StatesGroup):
    waiting_topic_name = State()
    waiting_discount_comment = State()

# ===== Вход в поддержку (ЛС) =====
@router.message(Command("support"))
async def cmd_support(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    await state.set_state(SupportFSM.wait_one_message)
    await m.answer(
        "Опиши проблему **одним сообщением** (можно приложить медиа).",
        parse_mode="Markdown",
        reply_markup=kb_to_main()
    )

# deep-link: /start support
@router.message(CommandStart(deep_link=True))
async def start_deeplink_support(m: Message, state: FSMContext):
    payload = ""
    if m.text and " " in m.text:
        payload = m.text.split(maxsplit=1)[1].strip()
    if payload.lower() != "support" or m.chat.type != "private":
        return
    await state.set_state(SupportFSM.wait_one_message)
    await m.answer(
        "Опиши проблему **одним сообщением** (можно приложить медиа).",
        parse_mode="Markdown",
        reply_markup=kb_to_main()
    )

@router.message(
    SupportFSM.wait_one_message,
    F.content_type.in_({ContentType.TEXT, ContentType.PHOTO, ContentType.DOCUMENT, ContentType.VIDEO, ContentType.AUDIO, ContentType.VOICE})
)
async def capture_user_message(m: Message, state: FSMContext):
    # Сбрасываем состояние после приёма одного сообщения
    await state.clear()

    # 1) Ищем «текущий» тикет, который надо продолжать (переживает рестарты)
    meta = await get_current_for_user(m.from_user.id, AUTOCLOSE_HOURS)
    if meta:
        ticket_id = meta["ticket_id"]
        thread_id = int(meta["thread_id"])

        async def _try_post_payload(tid: int):
            await post_user_payload_into_thread(m, tid)

        try:
            await _try_post_payload(thread_id)
            await m.answer("Спасибо! Твоё обращение дополнено 💌", reply_markup=kb_to_main())
            await update_ticket(ticket_id, updated_at=datetime.now(timezone.utc))
            return

        except TelegramBadRequest as e:
            if "message thread not found" in str(e).lower():
                # 2) Пересоздаём тему и переносим карточку
                title = f"{ticket_id} • @{m.from_user.username}" if m.from_user.username else f"{ticket_id} • {m.from_user.full_name}"
                new_thread_id = await create_forum_topic(m, title)
                
                # Проверка: thread_id должен быть валидным
                if new_thread_id is None or new_thread_id == 0:
                    raise ValueError(f"Пересоздана тема с невалидным thread_id={new_thread_id} для тикета {ticket_id}")

                header = ticket_header(ticket_id, m.from_user)
                card_msg = await send_card_in_thread(m, new_thread_id, header)

                # 2.1) Обновляем тикет в БД новым thread_id + card_msg_id
                try:
                    await update_ticket(ticket_id, thread_id=new_thread_id, card_msg_id=card_msg.message_id, updated_at=datetime.now(timezone.utc))
                except Exception:
                    pass

                # 2.2) Сообщение в General (если настроено), что тред был восстановлен
                try:
                    general_thread_id = await get_general_thread_id()
                    if general_thread_id:
                        await m.bot.send_message(
                            chat_id=SUPPORT_GROUP_ID,
                            message_thread_id=general_thread_id,
                            text=f"#{PROJECT_TAG} │ Тикет **{ticket_id}**: исходная тема недоступна, создан новый тред.",
                            parse_mode="Markdown",
                        )
                except Exception:
                    pass

                # 2.3) Повторяем отправку полезной нагрузки уже в новый тред
                await _try_post_payload(new_thread_id)

                await m.answer(
                    f"Создал новую тему для тикета `#{ticket_id}` и добавил ваше сообщение.",
                    parse_mode="Markdown",
                    reply_markup=kb_to_main()
                )
                return
            else:
                raise

    # 3) Создаём новый тикет (если текущего нет)
    ticket_id = secrets.token_hex(3).upper()
    title = f"{ticket_id} • @{m.from_user.username}" if m.from_user.username else f"{ticket_id} • {m.from_user.full_name}"

    # 3.1 Тема
    thread_id = await create_forum_topic(m, title)
    
    # Проверка: thread_id должен быть валидным (не None и не 0)
    if thread_id is None or thread_id == 0:
        raise ValueError(f"Создана тема с невалидным thread_id={thread_id} для тикета {ticket_id}")

    # 3.2 Карточка
    header = ticket_header(ticket_id, m.from_user)
    card_msg = await send_card_in_thread(m, thread_id, header)

    # 3.3 Вложение пользователя в тему
    await post_user_payload_into_thread(m, thread_id)

    # 3.4 Запись в General
    general_thread_id = await get_general_thread_id()
    general_msg_id = None
    if general_thread_id:
        gm = await m.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=general_thread_id,
            text=f"#{PROJECT_TAG} │ Тикет **{ticket_id}** создан",
            parse_mode="Markdown"
        )
        general_msg_id = gm.message_id

    # 3.5 Ответ пользователю
    await m.answer(
        "Спасибо! Твоё сообщение уже получено 💌."
        f"\nМенеджер {BRAND_NAME} скоро свяжется с тобой — обычно мы отвечаем в течение рабочего дня.",
        reply_markup=kb_to_main()
    )

    # 3.6 Сохраняем в БД
    await insert_ticket(
        ticket_id=ticket_id,
        user_id=m.from_user.id,
        thread_id=thread_id,
        status=TStatus.OPEN,
        general_msg_id=general_msg_id,
        card_msg_id=card_msg.message_id
    )

def ticket_header(ticket_id: str, user) -> str:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    user_block = f"Имя: {html.escape(user.full_name)}\n"
    if user.username:
        user_block += f"Username: @{html.escape(user.username)}\n"
    user_block += f"User ID: {user.id}\nПрофиль: tg://user?id={user.id}\n"
    return (
        f"[TICKET {ticket_id}][STATUS {TStatus.OPEN}][USER {user.id}]\n"
        f"#{PROJECT_TAG} • {when}\n"
        f"{user_block}"
        f"—\n"
        f"Ответ админа в этой теме уйдёт пользователю."
    )

# ===== Работа с темами форума =====
async def create_forum_topic(m: Message, title: str) -> int:
    """
    Создаёт новую тему форума и возвращает её message_thread_id.
    Выбрасывает исключение, если не удалось создать тему.
    """
    try:
        topic = await m.bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=title)
        thread_id = topic.message_thread_id
        if thread_id is None or thread_id == 0:
            raise ValueError(f"create_forum_topic вернул невалидный thread_id: {thread_id}")
        return thread_id
    except Exception as e:
        # Пробуем через call_api как fallback
        try:
            res = await m.bot.call_api("createForumTopic", {"chat_id": SUPPORT_GROUP_ID, "name": title})
            thread_id = int(res.get("message_thread_id", 0))
            if thread_id is None or thread_id == 0:
                raise ValueError(f"call_api вернул невалидный thread_id: {thread_id}")
            return thread_id
        except Exception as e2:
            # Если и fallback не сработал, пробрасываем исходную ошибку
            raise RuntimeError(f"Не удалось создать тему форума '{title}': {e}. Fallback также не сработал: {e2}") from e

async def close_forum_topic(m: Message, thread_id: int):
    try:
        await m.bot.close_forum_topic(chat_id=SUPPORT_GROUP_ID, message_thread_id=thread_id)
    except Exception:
        await m.bot.call_api("closeForumTopic", {"chat_id": SUPPORT_GROUP_ID, "message_thread_id": thread_id})

async def reopen_forum_topic(m: Message, thread_id: int):
    try:
        await m.bot.reopen_forum_topic(chat_id=SUPPORT_GROUP_ID, message_thread_id=thread_id)
    except Exception:
        await m.bot.call_api("reopenForumTopic", {"chat_id": SUPPORT_GROUP_ID, "message_thread_id": thread_id})

async def delete_forum_topic(m: Message, thread_id: int):
    try:
        await m.bot.delete_forum_topic(chat_id=SUPPORT_GROUP_ID, message_thread_id=thread_id)
    except Exception:
        await m.bot.call_api("deleteForumTopic", {"chat_id": SUPPORT_GROUP_ID, "message_thread_id": thread_id})

def extract_ticket_id(header_text: str) -> str:
    try:
        start = header_text.index("[TICKET ") + 8
        end = header_text.index("]", start)
        return header_text[start:end]
    except Exception:
        return "UNKNOWN"

async def post_user_payload_into_thread(m: Message, thread_id: int):
    # Проверка валидности thread_id
    if thread_id is None or thread_id == 0:
        raise ValueError(f"Попытка отправить сообщение в невалидный thread_id={thread_id}")
    
    async def _send():
        if m.photo:
            await m.bot.send_photo(SUPPORT_GROUP_ID, m.photo[-1].file_id, caption=m.caption or "", message_thread_id=thread_id)
        elif m.document:
            await m.bot.send_document(SUPPORT_GROUP_ID, m.document.file_id, caption=m.caption or "", message_thread_id=thread_id)
        elif m.video:
            await m.bot.send_video(SUPPORT_GROUP_ID, m.video.file_id, caption=m.caption or "", message_thread_id=thread_id)
        elif m.audio:
            await m.bot.send_audio(SUPPORT_GROUP_ID, m.audio.file_id, caption=m.caption or "", message_thread_id=thread_id)
        elif m.voice:
            await m.bot.send_voice(SUPPORT_GROUP_ID, m.voice.file_id, caption=m.caption or "", message_thread_id=thread_id)
        elif m.text:
            await m.bot.send_message(SUPPORT_GROUP_ID, m.text, message_thread_id=thread_id)
        else:
            await m.bot.send_message(SUPPORT_GROUP_ID, "Получено сообщение (тип не поддержан)", message_thread_id=thread_id)

    try:
        await _send()
    except TelegramBadRequest as e:
        # редкий лаг «message thread not found» сразу после создания темы — даём паузу и пробуем ещё раз
        if "message thread not found" in str(e).lower():
            await asyncio.sleep(0.8)
            await _send()
        else:
            raise

async def send_card_in_thread(m: Message, thread_id: int, header_text: str) -> Message:
    # Проверка валидности thread_id
    if thread_id is None or thread_id == 0:
        raise ValueError(f"Попытка отправить карточку в невалидный thread_id={thread_id}")
    
    try:
        return await m.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text=header_text,
            reply_markup=ticket_keyboard(extract_ticket_id(header_text), TStatus.OPEN)
        )
    except TelegramBadRequest as e:
        if "message thread not found" in str(e).lower():
            await asyncio.sleep(0.8)
            return await m.bot.send_message(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=thread_id,
                text=header_text,
                reply_markup=ticket_keyboard(extract_ticket_id(header_text), TStatus.OPEN)
            )
        else:
            raise

# ===== ADMIN -> USER: сообщения из темы уходят пользователю =====

# -- Обработка отмены редактирования темы --
@router.callback_query(F.data == "admin:cancel_edit")
async def on_admin_cancel_edit(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer("Редактирование отменено")

# -- Обработка ввода нового названия темы --
@router.message(SupportAdminFSM.waiting_topic_name)
async def on_admin_new_topic_name(m: Message, state: FSMContext):
    # Если это текстовое сообщение - меняем название
    if not m.text:
        await m.answer("Пожалуйста, отправьте текстовое название.")
        return

    data = await state.get_data()
    thread_id = data.get("admin_thread_id")
    
    # Пытаемся переименовать
    new_name = m.text.strip()
    try:
        # Используем editForumTopic (через bot.edit_forum_topic или call_api)
        # В aiogram 3.x метод edit_forum_topic есть у бота
        await m.bot.edit_forum_topic(chat_id=SUPPORT_GROUP_ID, message_thread_id=thread_id, name=new_name)
        await m.answer(f"✅ Тема переименована в: {new_name}")
    except Exception as e:
        await m.answer(f"❌ Не удалось переименовать тему: {e}")

    await state.clear()

# -- Обработка ввода комментария скидки --
@router.message(SupportAdminFSM.waiting_discount_comment)
async def on_admin_discount_comment(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("Пожалуйста, отправьте текстовый комментарий.")
        return

    comment = m.text.strip()
    data = await state.get_data()
    ticket_id = data.get("admin_ticket_id")
    
    # Получаем user_id из тикета
    meta = await get_by_ticket(ticket_id)
    if not meta:
        await m.answer("❌ Тикет не найден, не могу сохранить скидку.")
        await state.clear()
        return
        
    user_id = int(meta["user_id"])
    
    # Сохраняем в БД
    try:
        await set_user_discount(user_id, comment)
        await m.answer(f"✅ Комментарий к скидке сохранён для пользователя {user_id}:\n«{comment}»")
    except Exception as e:
        await m.answer(f"❌ Ошибка сохранения: {e}")

    await state.clear()


# флаги «подсказка уже отправлена после первого ответа» (в памяти процесса)
_first_reply_hint_sent: set[str] = set()

@router.message(F.chat.id == SUPPORT_GROUP_ID, F.message_thread_id.as_("thread_id"))
async def admin_message_router(m: Message, thread_id: int):
    if m.from_user and m.from_user.is_bot:
        return

    meta = await get_by_thread(thread_id)
    if not meta:
        return

    ticket_id = meta["ticket_id"]
    user_id = int(meta["user_id"])

    try:
        # 1) доставляем ответ пользователю
        if m.photo:
            await m.bot.send_photo(user_id, m.photo[-1].file_id, caption=(m.caption or "Ответ от поддержки: "))
        elif m.document:
            await m.bot.send_document(user_id, m.document.file_id, caption=(m.caption or "Ответ от поддержки: "))
        elif m.video:
            await m.bot.send_video(user_id, m.video.file_id, caption=(m.caption or "Ответ от поддержки: "))
        elif m.audio:
            await m.bot.send_audio(user_id, m.audio.file_id, caption=(m.caption or "Ответ от поддержки: "))
        elif m.voice:
            await m.bot.send_voice(user_id, m.voice.file_id, caption=(m.caption or "Ответ от поддержки: "))
        elif m.text:
            await m.bot.send_message(user_id, m.text)
        else:
            await m.bot.send_message(user_id, "Поддержка отправила ответ (тип вложения не поддержан).")

        # 1.1) ОДНОРАЗОВАЯ подсказка после первого ответа техподдержки
        if ticket_id not in _first_reply_hint_sent:
            _first_reply_hint_sent.add(ticket_id)
            hint_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛟 Перейти в «Поддержку»", callback_data="menu:support")]
            ])
            try:
                await m.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "Если хотите что-то добавить к обращению — перейдите в раздел **«Поддержка»** "
                        "и отправьте одно сообщение с деталями (можно с файлом)."
                    ),
                    parse_mode="Markdown",
                    reply_markup=hint_kb
                )
            except Exception:
                pass

        # 2) если был PENDING_USER/RESOLVED — вернуть в OPEN
        if meta.get("status") in (TStatus.PENDING_USER, TStatus.RESOLVED):
            await set_status_and_render(m, ticket_id, TStatus.OPEN, reason=None)

        # 3) touch активности
        await update_ticket(ticket_id, updated_at=datetime.now(timezone.utc))

    except Exception as e:
        await m.reply(f"Не удалось доставить ответ пользователю: {e}")

# ===== CALLBACKS (админские статусы) =====
@router.callback_query(F.data.startswith("t:"))
async def on_ticket_action(c: CallbackQuery, state: FSMContext):
    try:
        parts = c.data.split(":")
        ticket_id = parts[1]
        action = parts[2]
        if action == "set":
            new_status = parts[3]
            await set_status_and_render(c.message, ticket_id, new_status, reason=None)
            await c.answer("Статус обновлён")
        elif action == "close":
            await set_status_and_render(c.message, ticket_id, TStatus.CLOSED, reason="closed_by_admin")
            await c.answer("Тикет закрыт")
        elif action == "edit_topic":
            await c.answer()
            # Важно: thread_id берём из текущего сообщения (т.к. мы в топике)
            # Но если кнопка нажата не в топике (теоретически), то может быть проблема.
            # Полагаемся, что админ нажимает это внутри темы.
            current_thread_id = c.message.message_thread_id
            if not current_thread_id:
                # попытка фоллбэка через БД
                meta = await get_by_ticket(ticket_id)
                if meta:
                    current_thread_id = int(meta["thread_id"])
            
            if not current_thread_id:
                return await c.message.answer("Ошибка: не удалось определить thread_id для редактирования.")

            await state.set_state(SupportAdminFSM.waiting_topic_name)
            await state.update_data(admin_ticket_id=ticket_id, admin_thread_id=current_thread_id)
            
            kb_cancel = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅ Назад", callback_data="admin:cancel_edit")]
            ])
            await c.message.answer("Введите новое название темы:", reply_markup=kb_cancel)

        elif action == "add_discount":
            await c.answer()
            
            # Получаем текущий комментарий, если есть
            meta = await get_by_ticket(ticket_id)
            if not meta:
                return await c.message.answer("Тикет не найден.")
            
            user_id = int(meta["user_id"])
            current_discount = await get_user_discount(user_id)
            
            await state.set_state(SupportAdminFSM.waiting_discount_comment)
            await state.update_data(admin_ticket_id=ticket_id)

            kb_cancel = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅ Назад", callback_data="admin:cancel_edit")]
            ])
            
            text = "Введите комментарий (доп. скидка) для этого пользователя:"
            if current_discount:
                text += f"\n\nТекущая заметка: {current_discount}"
                
            await c.message.answer(text, reply_markup=kb_cancel)

        else:
            await c.answer("Неизвестное действие")
    except Exception as e:
        await c.answer(f"Ошибка: {e}", show_alert=True)

# ===== CALLBACKS (пользовательские 3 кнопки) =====
@router.callback_query(F.data.startswith("tu:"))
async def on_ticket_user_action(c: CallbackQuery, state: FSMContext):
    try:
        _, ticket_id, action = c.data.split(":")
        meta = await get_by_ticket(ticket_id)
        if not meta:
            return await c.answer("Тикет не найден", show_alert=True)

        user_id = int(meta["user_id"])
        thread_id = int(meta["thread_id"])

        if action == "ok":
            await set_status_and_render(c.message, ticket_id, TStatus.CLOSED, reason="closed_by_user")
            try:
                await delete_forum_topic(c.message, thread_id)
            except Exception:
                await close_forum_topic(c.message, thread_id)
            try:
                await c.message.edit_reply_markup(reply_markup=None)
            except:
                pass
            await c.message.answer(
                "🔥 Рады, что получилось решить вашу проблему! Если что — обращайтесь снова.",
                reply_markup=kb_to_main()
            )
            await update_ticket(ticket_id, updated_at=datetime.now(timezone.utc))
            return await c.answer("Тикет закрыт")

        if action == "notok":
            await set_status_and_render(c.message, ticket_id, TStatus.OPEN, reason="reopen_by_user")
            try:
                await reopen_forum_topic(c.message, thread_id)
            except Exception:
                pass
            try:
                await c.message.edit_reply_markup(reply_markup=None)
            except:
                pass
            # снова ждём одно сообщение от пользователя
            await state.set_state(SupportFSM.wait_one_message)
            await c.message.answer(
                "Понял, продолжаем. Опишите, пожалуйста, что именно осталось нерешённым.",
                reply_markup=kb_to_main()
            )
            try:
                await c.message.bot.send_message(
                    chat_id=SUPPORT_GROUP_ID,
                    message_thread_id=thread_id,
                    text="🔔 Пользователь указал, что проблема **не решена**. Продолжаем обработку."
                )
            except:
                pass
            await update_ticket(ticket_id, updated_at=datetime.now(timezone.utc))
            return await c.answer("Тикет переоткрыт")

        if action == "back":
            try:
                await c.message.edit_reply_markup(reply_markup=None)
            except:
                pass
            await send_main_menu_inline(c.message)
            return await c.answer("Открыл меню")

        await c.answer("Неизвестное действие", show_alert=True)

    except Exception as e:
        await c.answer(f"Ошибка: {e}", show_alert=True)

# ===== ДОБАВЛЕНО: обработчик «в главное меню» =====
@router.callback_query(F.data == CB_TO_MAIN)
async def on_to_main(c: CallbackQuery, state: FSMContext):
    await c.answer()
    try:
        await c.message.delete()
    except Exception:
        pass
    try:
        await state.clear()
    except Exception:
        pass
    await send_main_menu_inline(c.message)

# ===== Изменение статуса + General =====
async def set_status_and_render(msg: Message, ticket_id: str, status: str, reason: Optional[str]):
    meta = await get_by_ticket(ticket_id)
    if not meta:
        return

    thread_id = int(meta["thread_id"])
    card_msg_id = int(meta["card_msg_id"])
    general_msg_id_raw = meta.get("general_msg_id")
    general_msg_id = int(general_msg_id_raw) if general_msg_id_raw else 0
    user_id = int(meta["user_id"])

    # 1) карточка в теме
    try:
        new_header = (
            f"[TICKET {ticket_id}][STATUS {status}][USER {user_id}]\n"
            f"#{PROJECT_TAG} • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"—\n"
            f"{status_badge(status)}"
        )
        await msg.bot.edit_message_text(
            chat_id=SUPPORT_GROUP_ID,
            message_id=card_msg_id,
            text=new_header,
            reply_markup=ticket_keyboard(ticket_id, status)
        )
    except Exception:
        pass

    # 2) запись в General (через .env-тему)
    general_thread_id = await get_general_thread_id()
    if general_thread_id:
        suffix = f" (причина: {reason})" if status == TStatus.CLOSED and reason else ""
        new_text = (
            f"#{PROJECT_TAG} │ Тикет **{ticket_id}** "
            f"{'закрыт' if status == TStatus.CLOSED else 'обновлён'}: {status_badge(status)}{suffix}"
        )
        if general_msg_id:
            try:
                await msg.bot.edit_message_text(
                    chat_id=SUPPORT_GROUP_ID,
                    message_id=general_msg_id,
                    text=new_text,
                    parse_mode="Markdown",
                )
            except Exception:
                # если не редактируется — создаём новую запись
                try:
                    gm = await msg.bot.send_message(
                        chat_id=SUPPORT_GROUP_ID,
                        message_thread_id=general_thread_id,
                        text=new_text,
                        parse_mode="Markdown",
                    )
                    await update_ticket(ticket_id, general_msg_id=gm.message_id)
                except Exception:
                    pass
        else:
            try:
                gm = await msg.bot.send_message(
                    chat_id=SUPPORT_GROUP_ID,
                    message_thread_id=general_thread_id,
                    text=new_text,
                    parse_mode="Markdown",
                )
                await update_ticket(ticket_id, general_msg_id=gm.message_id)
            except Exception:
                pass

    # 3) эффекты по статусу
    if status == TStatus.RESOLVED:
        try:
            await msg.bot.send_message(
                user_id,
                f"🟢 По тикету `#{ticket_id}` предложено решение. "
                f"Если проблема не решена — ответьте в этом чате или воспользуйтесь кнопками ниже.",
                parse_mode="Markdown",
                reply_markup=ticket_resolved_feedback_inline(ticket_id)
            )
        except:
            pass
        asyncio.create_task(schedule_autoclose(msg, ticket_id, hours=AUTOCLOSE_HOURS))

    if status == TStatus.CLOSED:
        try:
            # ВНИМАНИЕ: по требованию — БЕЗ кнопки «в главное меню»
            await msg.bot.send_message(
                user_id,
                f"🔒 Тикет `#{ticket_id}` закрыт. Для новой темы просто напишите снова — будет создан новый тикет.",
                parse_mode="Markdown"
            )
        except:
            pass
        await close_forum_topic(msg, thread_id)

    # 4) сохраняем статус + touch
    await update_ticket(ticket_id, status=status, updated_at=datetime.now(timezone.utc))

async def schedule_autoclose(msg: Message, ticket_id: str, hours: int):
    await asyncio.sleep(hours * 3600)
    meta = await get_by_ticket(ticket_id)
    if not meta:
        return
    if meta.get("status") == TStatus.RESOLVED:
        await set_status_and_render(msg, ticket_id, TStatus.CLOSED, reason="auto_after_resolved")

# ==== Диагностика и проверка ====
@router.message(Command("whereami"))
async def whereami(m: Message):
    chat_type = getattr(m.chat, "type", None)
    is_forum = getattr(m.chat, "is_forum", None)
    await m.answer(
        f"chat_id={m.chat.id}\n"
        f"chat.type={chat_type}\n"
        f"chat.is_forum={is_forum}\n"
        f"is_topic_message={m.is_topic_message}\n"
        f"message_thread_id={m.message_thread_id}"
    )

@router.message(Command("test_general"))
async def test_general(m: Message):
    if not SUPPORT_GROUP_ID:
        return await m.answer("Не задан SUPPORT_GROUP_ID в .env")
    thread_id = await get_general_thread_id()
    if not thread_id:
        return await m.answer("Не задан GENERAL_THREAD_ID в .env")
    try:
        await m.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text="Тестовое сообщение в General/Statuses ✅"
        )
        await m.answer(f"Отправил тест в General/Statuses (chat_id={SUPPORT_GROUP_ID}, thread_id={thread_id}).")
    except Exception as e:
        await m.answer(f"Не удалось отправить в General: {e}")

# ==== Инициализация хранилища (вызвать из main) ====
async def init_support_storage():
    await support_init_tables()

# ==== Публичные хелперы для main ====
async def user_open_ticket_id(user_id: int) -> Optional[str]:
    data = await get_current_for_user(user_id, AUTOCLOSE_HOURS)
    return data["ticket_id"] if data else None

async def enter_support_from_menu(message: Message, state: FSMContext):
    data = await get_current_for_user(message.from_user.id, AUTOCLOSE_HOURS)
    if data:
        await state.set_state(SupportFSM.wait_one_message)
        await message.answer(
            f"По вашему обращению уже открыт тикет `#{data['ticket_id']}`.\n"
            f"Если хотите добавить детали — пришлите **одно сообщение** (можно с файлом), и я прикреплю его к действующему тикету.",
            parse_mode="Markdown",
            reply_markup=kb_to_main()
        )
    else:
        await state.set_state(SupportFSM.wait_one_message)
        await message.answer(
            "Если у тебя есть вопрос по заказу, размеру или качеству — наш менеджер всегда на связи 💬."
            f"\n Можешь прикрепить фото или видео, чтобы мы быстрее помогли разобраться в ситуации."
            f"\n Ответим в ближайшее время и решим всё максимально удобно для тебя 💛",
            parse_mode="Markdown",
            reply_markup=kb_to_main()
        )
