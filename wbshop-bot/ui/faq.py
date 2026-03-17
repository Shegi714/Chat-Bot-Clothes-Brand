# ui/faq.py
from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import BRAND_NAME

router = Router(name="faq")

# ----- Контент FAQ -----
FAQ: dict[str, dict[str, str]] = {
    "about_brand": {
        "title": "О бренде",
        "text": (
            "– Что это за бренд?\n"
            "Наш бренд создаёт современную базовую одежду, которая сочетает комфорт, универсальность и стиль. "
            "Мы помогаем женщинам собирать капсульный гардероб, чтобы выглядеть стильно каждый день — без лишних усилий.\n\n"
            "– В чём особенность вашей одежды?\n"
            "Мы используем мягкие и приятные ткани, продумываем каждую деталь кроя, чтобы вещи были удобными и современными. "
            "Наша одежда легко комбинируется и подходит для работы, прогулок и отдыха.\n\n"
            "– Для кого подойдёт ваша продукция?\n"
            "Для женщин 25–50 лет, которые ценят комфорт, минимализм и универсальность в гардеробе."
        ),
    },
    "products": {
        "title": "О продукции",
        "text": (
            "– Это базовая одежда?\n"
            "Да, каждая вещь универсальна и легко впишется в капсульный гардероб. "
            "Вы сможете создавать десятки стильных образов с минимальными усилиями.\n\n"
            "– Как выбрать размер?\n"
            "В карточке товара на маркетплейсе всегда есть размерная сетка. Если сомневаетесь — выбирайте тот размер, "
            "который обычно носите, или ориентируйтесь по обхвату груди, талии и бёдер.\n\n"
            "– Как ухаживать за вещами?\n"
            "Все рекомендации указаны на бирке и в карточке товара. Обычно достаточно деликатной стирки при низкой температуре, "
            "чтобы сохранить качество ткани и цвет."
        ),
    },
    "cashback": {
        "title": "О кэшбэке за отзыв",
        "text": (
            "– Как получить кэшбэк за отзыв?\n\n"
            "Купите товар и оставьте отзыв на маркетплейсе.\n\n"
            "Отправьте чек о покупке в наш Telegram-бот.\n\n"
            "Бот автоматически найдёт ваш отзыв и передаст данные в бухгалтерию.\n\n"
            "Получите кэшбэк удобным способом! 🎉\n\n"
            "– Нужен ли скриншот отзыва?\n"
            "Нет, достаточно только чека — бот сам всё проверит."
        ),
    },
    "promos": {
        "title": "Об акциях и розыгрышах",
        "text": (
            "– Проводите ли вы розыгрыши и акции?\n"
            "Да, мы регулярно радуем покупателей подарками и спецпредложениями. 🎁\n\n"
            "– Как принять участие?\n"
            "Условия мы публикуем в карточках товаров и в социальных сетях бренда. Обычно всё очень просто: "
            "купить товар → выполнить лёгкое условие (например, оставить отзыв) → участвовать в розыгрыше."
        ),
    },
    "delivery": {
        "title": "Доставка и возврат",
        "text": (
            "– Как происходит доставка?\n"
            "Мы работаем через маркетплейсы (Ozon, Wildberries), поэтому вы можете выбрать удобный пункт выдачи или доставку курьером.\n\n"
            "– Можно ли вернуть товар?\n"
            "Да, возврат оформляется через личный кабинет маркетплейса в установленные сроки."
        ),
    },
    "more": {
        "title": "Дополнительно",
        "text": (
            "– Чем ваш бренд отличается от других?\n"
            "Мы делаем ставку на универсальность, комфорт и доступность. Наши вещи легко сочетаются, создают готовые образы "
            "и помогают выглядеть стильно каждый день.\n\n"
            "– Где можно следить за новостями бренда?\n"
            "Подписывайтесь на нас в социальных сетях и не пропустите новые коллекции, акции и розыгрыши."
        ),
    },
    "consent": {
        "title": "Согласие на обработку персональных данных",
        "text": (
            "Демо-согласие на обработку персональных данных\n\n"
            f"Продолжая использование бота «{BRAND_NAME}», вы соглашаетесь на обработку персональных данных "
            "(например, имя и номер телефона) в целях обработки заявок/обращений и осуществления выплат/вознаграждений.\n\n"
            "Это примерный текст для публичного репозитория. В реальном проекте замените его на юридически корректный документ "
            "и укажите реквизиты оператора персональных данных."
        ),
    },
    "privacy": {
        "title": "Политика конфиденциальности и обработки персональных данных",
        "text": (
            f"(для пользователей бота/сервиса «{BRAND_NAME}»)\n\n"
            "Это демо-политика для публичного репозитория.\n\n"
            "1. Какие данные обрабатываются\n"
            "- имя (которое Telegram передаёт боту)\n"
            "- номер телефона (если пользователь вводит его для выплаты/заявки)\n\n"
            "2. Зачем\n"
            "- обработка обращений в поддержку\n"
            "- обработка заявок на кэшбек/выплаты\n\n"
            "3. Хранение и защита\n"
            "- данные хранятся только в рамках работы демо\n"
            "- доступы/ключи выносятся в переменные окружения (.env)\n\n"
            "В реальном проекте замените этот раздел на юридически корректный документ и укажите контакты оператора."
        ),
    },
}

# ----- Клавиатуры -----
def _topics_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="О бренде", callback_data="faq:topic:about_brand")],
        [InlineKeyboardButton(text="О продукции", callback_data="faq:topic:products")],
        [InlineKeyboardButton(text="О кэшбэке за отзыв", callback_data="faq:topic:cashback")],
        [InlineKeyboardButton(text="Об акциях и розыгрышах", callback_data="faq:topic:promos")],
        [InlineKeyboardButton(text="Доставка и возврат", callback_data="faq:topic:delivery")],
        [InlineKeyboardButton(text="Дополнительно", callback_data="faq:topic:more")],
        [InlineKeyboardButton(text="Согласие на обработку ПДн", callback_data="faq:topic:consent")],
        [InlineKeyboardButton(text="Политика конфиденциальности", callback_data="faq:topic:privacy")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="menu:back")],  # назад в Главное меню
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _topic_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅ Назад к темам", callback_data="faq:back_topics")]
    ])

# ----- Рендер -----
async def send_faq_topics(message: Message):
    text = "FAQ — выберите тему:"
    try:
        # пробуем обновить предыдущее сообщение
        await message.edit_text(text, reply_markup=_topics_kb())
    except Exception:
        await message.answer(text, reply_markup=_topics_kb())

async def send_faq_topic(message: Message, key: str):
    item = FAQ.get(key)
    if not item:
        return await send_faq_topics(message)
    text = f"🧾 {item['title']}\n\n{item['text']}"
    try:
        await message.edit_text(text, reply_markup=_topic_back_kb())
    except Exception:
        await message.answer(text, reply_markup=_topic_back_kb())

# ----- Хендлеры -----
@router.callback_query(F.data == "menu:faq")
async def on_faq_root(call: CallbackQuery):
    await send_faq_topics(call.message)
    await call.answer()

@router.callback_query(F.data == "faq:back_topics")
async def on_faq_back_topics(call: CallbackQuery):
    await send_faq_topics(call.message)
    await call.answer()

@router.callback_query(F.data.startswith("faq:topic:"))
async def on_faq_topic(call: CallbackQuery):
    key = call.data.split("faq:topic:", 1)[-1]
    await send_faq_topic(call.message, key)
    await call.answer()
