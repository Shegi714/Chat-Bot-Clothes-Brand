# ui/menu.py
from aiogram.types import Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from wbshop_bot.config import BRAND_NAME, BRAND_SITE_URL

def main_menu_inline() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛟 Поддержка", callback_data="menu:support")
    kb.button(text="🎁 Получить кэшбек", callback_data="bonus:start")  # ведёт в bonus.py
    kb.button(text=f"👥 Комьюнити {BRAND_NAME}", callback_data="menu:community")
    kb.button(text=f"🌐 Сайт {BRAND_NAME}", url=BRAND_SITE_URL)
    kb.button(text=f"🛍 Каталог {BRAND_NAME}", callback_data="menu:catalog")
    kb.button(text="🤝 Сотрудничество", callback_data="menu:partner")
    kb.button(text="🔔 Уведомления", callback_data="menu:notify")
    kb.button(text="❓ FAQ", callback_data="menu:faq")
    kb.adjust(1)
    return kb.as_markup()

async def send_main_menu_inline(message: Message) -> None:
    await message.answer("Главное меню:", reply_markup=main_menu_inline())

def ticket_resolved_feedback_inline(ticket_id: str) -> InlineKeyboardMarkup:
    """
    Кнопки под сообщением пользователю после статуса RESOLVED:
    1) Проблема решена
    2) Проблема не решена
    3) Назад (в главное меню)
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Проблема решена",   callback_data=f"tu:{ticket_id}:ok")
    kb.button(text="❗ Проблема не решена", callback_data=f"tu:{ticket_id}:notok")
    kb.button(text="⬅ Назад",              callback_data=f"tu:{ticket_id}:back")
    kb.adjust(1)  # по одному в столбик, как в главном меню
    return kb.as_markup()