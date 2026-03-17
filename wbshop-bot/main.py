import os
import asyncio
import pathlib

from config import (
    BOT_TOKEN,
    BRAND_NAME,
    BOT_FALLBACK_USERNAME,
    COMMUNITY_URL,
    CATALOG_WB_URL,
    CATALOG_OZON_URL,
    CATALOG_YM_URL,
)

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage  # хранилище FSM

# агенты
from agent_orders import daily_orders_agent
from agent_reviews import daily_reviews_agent

# БД
from db import engine, DATABASE_URL
from models import Base

# главное меню (совместимо с кодом из ui/menu)
from ui.menu import main_menu_inline, send_main_menu_inline
# FAQ роутер + доступ к контенту (берём текст для "consent")
from ui.faq import router as faq_router, FAQ

# форумы поддержки (Variant B: тема на тикет + General)
from support_forum import (
    router as support_forum_router,
    enter_support_from_menu,
)

# раздел «Сотрудничество»
from ui.partner import router as partner_router

# раздел «Персональные уведомления»
from ui.notify import router as notify_router, init_notify_storage

# ✅ репозиторий подписок уведомлений (новый файл)
from ui.notify_repo import ensure_notify_on  # автоподписываем только при /start

# support repo (DDL fallback)
from support_repo import init_tables as support_init_tables

# cashback router
from bonus import router as bonus_router

# Пытаемся подтянуть init_support_storage из support_forum, если он там есть
try:
    from support_forum import init_support_storage  # type: ignore
except Exception:
    init_support_storage = None  # будет fallback на support_repo.init_tables

router = Router()

# --- ССЫЛКИ БРЕНДА/КАТАЛОГА ---

# --- клавиатуры ---

def catalog_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Wildberries",     url=CATALOG_WB_URL)],
        [InlineKeyboardButton(text="Ozon",           url=CATALOG_OZON_URL)],
        [InlineKeyboardButton(text="Яндекс Маркет",  url=CATALOG_YM_URL)],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="menu:back")],
    ])

def community_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вступить в комьюнити", url=COMMUNITY_URL)],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="menu:back")],
    ])

def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🚀 Старт")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Нажмите «Старт» чтобы открыть меню"
    )

# --- базовые хендлеры ---

@router.message(CommandStart())
async def cmd_start(message: Message):
    # deep-link payload: /start <payload>
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(maxsplit=1)[1].strip()

    # Если deep-link "faq_consent" — просто присылаем текст
    if payload == "faq_consent":
        consent = FAQ.get("consent", {"title": "Согласие на обработку ПДн", "text": ""})
        text = f"{consent['title']}\n\n{consent['text']}"
        await message.answer(text)
        return

    # ✅ ВКЛЮЧАЕМ УВЕДОМЛЕНИЯ ПО УМОЛЧАНИЮ (upsert enabled=1) ТОЛЬКО ПРИ /start
    try:
        await ensure_notify_on(message.from_user.id)
    except Exception:
        # не валим /start, чтобы пользователь всё равно увидел приветствие
        pass

    # Стартовое сообщение с ссылкой на deep-link
    me = await message.bot.get_me()
    bot_username = me.username or BOT_FALLBACK_USERNAME
    consent_link = f"https://t.me/{bot_username}?start=faq_consent"

    await message.answer(
        f"Привет! Это бот {BRAND_NAME}.\n"
        "Используя бота, вы соглашаетесь на обработку персональных данных (например, имя и телефон) "
        "в целях обработки заявок/выплат и предоставления информации. Подробнее — в "
        f"[политике и согласии]({consent_link}). Если вы не согласны, прекратите использование бота.",
        reply_markup=main_reply_kb(),
        parse_mode="Markdown"
    )

@router.message(F.text.in_({"🚀 Старт", "Старт"}))
async def on_start_button(message: Message):
    # ❌ НИЧЕГО НЕ МЕНЯЕМ В ПОДПИСКЕ ЗДЕСЬ
    # (чтобы не перезатирать ручное отключение уведомлений)
    await send_main_menu_inline(message)

@router.callback_query(F.data == "menu:support")
async def cb_support(call: CallbackQuery, state: FSMContext):
    """
    «Умная» поддержка:
    - нет открытого тикета → просим одно сообщение (включаем FSM);
    - есть открытый → сообщаем номер и ждём одно сообщение (уйдёт в тему).
    """
    await enter_support_from_menu(call.message, state)
    await call.answer()

@router.callback_query(F.data == "menu:catalog")
async def cb_catalog(call: CallbackQuery):
    text = (
        "Каталог доступен на маркетплейсах.\n"
        f"Выберите площадку {BRAND_NAME}:"
    )
    try:
        await call.message.edit_text(text, reply_markup=catalog_menu_inline())
    except Exception:
        await call.message.answer(text, reply_markup=catalog_menu_inline())
    await call.answer()

@router.callback_query(F.data == "menu:community")
async def cb_community(call: CallbackQuery):
    text = (
        f"Сообщество {BRAND_NAME} — новости, анонсы и полезные материалы.\n"
    )
    try:
        await call.message.edit_text(text, reply_markup=community_menu_inline())
    except Exception:
        await call.message.answer(text, reply_markup=community_menu_inline())
    await call.answer()

@router.callback_query(F.data == "menu:back")
async def cb_back(call: CallbackQuery):
    try:
        await call.message.edit_text(
            "Главное меню",
            reply_markup=main_menu_inline()
        )
    except Exception:
        await send_main_menu_inline(call.message)
    await call.answer()

# --- БД: автоинициализация ---

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[db] DATABASE_URL =", DATABASE_URL)
    if DATABASE_URL.startswith("sqlite"):
        dbfile = DATABASE_URL.split("///", 1)[-1]
        print("[db] sqlite file abs path =", pathlib.Path(dbfile).resolve())

# --- запуск ---

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в .env")

    # 1) инициализируем основную БД (ORM-модели проекта)
    await init_db()

    # 2) создаём таблицу support_tickets (DDL).
    if init_support_storage:
        await init_support_storage()
    else:
        await support_init_tables()

    # 2.1) создаём таблицу notify_subs для уведомлений (если нужен DDL)
    await init_notify_storage()

    # 3) инициализация бота/диспетчера с FSM-хранилищем
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())  # важно для FSM

    # порядок подключения не критичен
    dp.include_router(router)                 # базовый роутер (меню, старт)
    dp.include_router(faq_router)             # FAQ роутер
    dp.include_router(support_forum_router)   # Форумная техподдержка
    dp.include_router(bonus_router)           # кэшбек
    dp.include_router(partner_router)         # меню сотрудничества
    dp.include_router(notify_router)          # персональные уведомления

    # фоновые агенты
    asyncio.create_task(daily_orders_agent())
    asyncio.create_task(daily_reviews_agent())

    print("Bot started. Press Ctrl+C to stop.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
