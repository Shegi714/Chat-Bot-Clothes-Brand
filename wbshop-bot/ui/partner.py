# ui/partner.py
from __future__ import annotations
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BRAND_NAME, PARTNER_FORM_URL

router = Router(name="partner")

# =========================
# Константы и тексты (редактируй тут)
# =========================

GOOGLE_FORM_URL = PARTNER_FORM_URL  # ссылка на форму заявки (настройка через env)

PARTNER_INTRO_TEXT = (
    f"{BRAND_NAME} — демо-бренд для примера, который показывает структуру бота.  \n"
    "Если ты блогер, креатор, модель, фотограф, видеограф или менеджер маркетплейсов — нам есть что делать вместе 💫  \n\n"
    "Мы открыты к коллаборациям, съёмкам, амбассадорским проектам и новым партнёрствам.  \n"
    f"Оставь заявку — команда {BRAND_NAME} свяжется с тобой лично."
)

# Описания разделов — заполни своими текстами
DESC_MODELS = f"Мы ищем моделей и фотографов для съёмок lookbook и контент-дней {BRAND_NAME}.  \nЕсли тебе близка эстетика бренда — заполни короткую анкету."
DESC_VIDEO = f"{BRAND_NAME} создаёт атмосферные видео-кампании и fashion-контент. \nИщем видеографов с чувством кадра и стиля."
DESC_MANAGERS = f"{BRAND_NAME} открыта к сотрудничеству с менеджерами маркетплейсов — \nпо ведению карточек, аналитике и продвижению. \nЕсли тебе откликается наш подход — оставь заявку."
DESC_BLOGGERS = f"Если ты вдохновляешь людей и чувствуешь стиль — \nдавай создавать контент вместе. \n{BRAND_NAME} поддерживает коллаборации и амбассадорские проекты."

# =========================
# Клавиатуры
# =========================

def kb_partner_root() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # кнопка-заявка (ссылка сразу на форму)
    kb.button(text="🤝 Оставить заявку на сотрудничество", url=GOOGLE_FORM_URL)
    # подпункты (по колбэку, открываем экран с описанием + кнопка «Оставить заявку»)
    kb.button(text="📸 Для моделей и фотографов", callback_data="partner:models")
    kb.button(text="🎥 Для видеографов", callback_data="partner:video")
    kb.button(text="🧾 Для менеджеров WB / Ozon", callback_data="partner:managers")
    kb.button(text="🌟 Для блогеров и креаторов", callback_data="partner:bloggers")
    # назад — на уровень ниже (в твой общий хендлер "menu:back" → главное меню)
    kb.button(text="⬅ Назад", callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()

def kb_partner_apply_back() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Оставить заявку", url=GOOGLE_FORM_URL)
    kb.button(text="⬅ Назад", callback_data="partner:back")
    kb.adjust(1, 1)
    return kb.as_markup()

# =========================
# Хендлеры
# =========================

@router.callback_query(F.data == "menu:partner")
async def partner_root(call: CallbackQuery):
    try:
        await call.message.edit_text(PARTNER_INTRO_TEXT, reply_markup=kb_partner_root())
    except Exception:
        await call.message.answer(PARTNER_INTRO_TEXT, reply_markup=kb_partner_root())
    await call.answer()

@router.callback_query(F.data == "partner:back")
async def partner_back(call: CallbackQuery):
    # Возврат на корень раздела сотрудничества
    try:
        await call.message.edit_text(PARTNER_INTRO_TEXT, reply_markup=kb_partner_root())
    except Exception:
        await call.message.answer(PARTNER_INTRO_TEXT, reply_markup=kb_partner_root())
    await call.answer()

@router.callback_query(F.data == "partner:models")
async def partner_models(call: CallbackQuery):
    try:
        await call.message.edit_text(DESC_MODELS, reply_markup=kb_partner_apply_back())
    except Exception:
        await call.message.answer(DESC_MODELS, reply_markup=kb_partner_apply_back())
    await call.answer()

@router.callback_query(F.data == "partner:video")
async def partner_video(call: CallbackQuery):
    try:
        await call.message.edit_text(DESC_VIDEO, reply_markup=kb_partner_apply_back())
    except Exception:
        await call.message.answer(DESC_VIDEO, reply_markup=kb_partner_apply_back())
    await call.answer()

@router.callback_query(F.data == "partner:managers")
async def partner_managers(call: CallbackQuery):
    try:
        await call.message.edit_text(DESC_MANAGERS, reply_markup=kb_partner_apply_back())
    except Exception:
        await call.message.answer(DESC_MANAGERS, reply_markup=kb_partner_apply_back())
    await call.answer()

@router.callback_query(F.data == "partner:bloggers")
async def partner_bloggers(call: CallbackQuery):
    try:
        await call.message.edit_text(DESC_BLOGGERS, reply_markup=kb_partner_apply_back())
    except Exception:
        await call.message.answer(DESC_BLOGGERS, reply_markup=kb_partner_apply_back())
    await call.answer()
