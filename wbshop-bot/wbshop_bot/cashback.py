# bonus.py
from __future__ import annotations
from wbshop_bot.ui.menu import send_main_menu_inline as send_main_menu

import os
import re
import json
import base64
import tempfile
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional, Dict, Any, Iterable
from decimal import Decimal

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ContentType, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.base import StorageKey
from aiogram.utils.keyboard import InlineKeyboardBuilder

import aiohttp  # ⬅️ для HTML-фолбэка
import secrets

LOG_LEVEL = os.getenv("BONUS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] bonus: %(message)s",
)
logger = logging.getLogger("bonus")

router = Router(name="bonus")

try:
    from wbshop_bot.storage.db import async_session_maker  # type: ignore
except Exception:
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from wbshop_bot.storage.db import engine  # type: ignore
    async_session_maker = async_sessionmaker(engine, expire_on_commit=False)  # type: ignore

from wbshop_bot.storage.dao import (
    srid_core,
    find_orders_by_srids_fuzzy,
    find_reviews_for_orders,
    get_claimed_srids,
    insert_claims_for_orders,
    get_user_discount,  # добавили
    set_user_discount,
)
from wbshop_bot.storage.models import Order, Review
from wbshop_bot.services.receipts import (
    extract_srid_from_pdf,
    extract_srids_from_url,
    extract_srids_from_url_async_all,
    extract_srids_from_text,
)

# ==== интеграция с тикетами поддержки
from wbshop_bot.support.forum import (
    create_forum_topic,
    send_card_in_thread,
    post_user_payload_into_thread,
    ticket_header,
    set_status_and_render,
    TStatus,
    SUPPORT_GROUP_ID,  # chat_id чата поддержки
)
from wbshop_bot.support.repo import insert_ticket

VALIDATE_ORDER_MAX_AGE_DAYS = int(os.getenv("BONUS_ORDER_MAX_AGE_DAYS", "0"))
BONUS_MAX_SRIDS = int(os.getenv("BONUS_MAX_SRIDS", "0"))
MIN_GOOD_RATING = int(os.getenv("BONUS_MIN_GOOD_RATING", "5"))

POSITIVE_REPLY = "Заказ найден, отзыв проверен"
NEGATIVE_REPLY = "С заказом что-то не так, обратитесь в службу поддержки"
NO_ORDER_REPLY = "По вашему заказу невозможно получить кэшбек"
ALREADY_CLAIMED_REPLY = "Данный чек уже был обработан"

CB_START   = "bonus:start"
CB_BACK    = "bonus:back"
CB_REVIEW  = "bonus:review"
CB_REPOST  = "bonus:repost"

CB_HOWTO   = "bonus:howto"
CB_RETRY   = "bonus:retry"
CB_SUPPORT = "bonus:support"

CB_BANK_SBER = "bonus:bank:sber"
CB_BANK_T    = "bonus:bank:tbank"
CB_BANK_ALFA = "bonus:bank:alfa"
CB_TO_MAIN   = "bonus:to_main"
BANK_CB_SET  = {CB_BANK_SBER, CB_BANK_T, CB_BANK_ALFA}

CB_START_ALIASES = {
    CB_START,
    "menu:bonus", "menu:get_bonus", "main:get_bonus",
    "get_bonus", "open_bonus", "action:bonus", "go_bonus",
}

GET_BONUS_TEXTS = {"получить кэшбек", "🎁 получить кэшбек", "/bonus"}

# === HOWTO-видео (инструкция) ===
HOWTO_VIDEO_FILE_ID = os.getenv("HOWTO_VIDEO_FILE_ID", "").strip()
HOWTO_VIDEO_PATH = os.getenv("HOWTO_VIDEO_PATH", "instruction.mp4")
HOWTO_VIDEO_CACHE_JSON = os.getenv("HOWTO_VIDEO_CACHE_JSON", "data/howto_video.json")
HOWTO_VIDEO_ADMIN_USERNAME = os.getenv("HOWTO_VIDEO_ADMIN_USERNAME", "").strip()
HOWTO_CAPTION = "Инструкция: как получить чек и прислать его сюда."

class BonusFSM(StatesGroup):
    waiting_receipt = State()  # ⬅️ новое состояние
    waiting_phone   = State()
    waiting_bank    = State()

# === FSM для поддержки: ждём скрин/подтверждение отзыва после нажатия «в поддержку»
class SupportStates(StatesGroup):
    AwaitReviewProof = State()

# Память контекста кэшбекных тикетов (в процессе; перезапуск бота очистит её)
BONUS_TICKET_CTX: Dict[str, Dict[str, Any]] = {}

# ---------- НОВЫЕ: SRID-помощники (универсальный парсер) ----------
_HEX32_WITH_OPT = r"\b[a-f0-9]{32}(?:\.\d+\.\d+)?\b"
_DPREFIX = r"\bd[ucb]\.[a-z0-9]{6,}(?:\.[a-z0-9]+)*\b"
_NUM_WITH_TAIL = r"\b\d{8,}\.\d+\.\d+\b"
_NUM_CORE_ONLY = r"\b\d{17,20}\b"

_SRID_ANY_RE = re.compile(
    rf"(?:{_DPREFIX}|{_HEX32_WITH_OPT}|{_NUM_WITH_TAIL}|{_NUM_CORE_ONLY})",
    re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]+>")

def _uniq_preserve(seq: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in seq:
        if not x:
            continue
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out

def _expand_with_core(srids: Iterable[str]) -> List[str]:
    out: List[str] = []
    used = set()
    for s in srids:
        if not s:
            continue
        s1 = str(s)
        c1 = srid_core(s1)
        for v in (s1, c1):
            if v and v not in used:
                out.append(v)
                used.add(v)
    return out

def extract_srids_loose_from_text(txt: str) -> List[str]:
    if not txt:
        return []
    base = extract_srids_from_text(txt) or []
    extra = _SRID_ANY_RE.findall(txt)
    return _uniq_preserve([*base, *extra])

async def fetch_html_and_extract_srids(url: str) -> List[str]:
    try:
        timeout = aiohttp.ClientTimeout(total=25, sock_connect=10, sock_read=20)
        connector = aiohttp.TCPConnector(ttl_dns_cache=60)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    return []
                html = await r.text(errors="ignore")
    except Exception:
        return []
    from html import unescape
    text = _TAG_RE.sub(" ", html)
    text = re.sub(r"\s+", " ", unescape(text))
    return _uniq_preserve(_SRID_ANY_RE.findall(text))

def _to_dec(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None

def _pick_order_sum(o: Order) -> Optional[Any]:
    for name in ("amount_rub", "sum", "amount", "price", "price_with_disc", "total_sum", "total", "payment"):
        if hasattr(o, name):
            val = getattr(o, name)
            if val is not None:
                return val
    return None

def _order_to_dict(o: Order) -> Dict[str, Any]:
    return {
        "id": getattr(o, "id", None),
        "srid": getattr(o, "srid", None),
        "nmId": getattr(o, "product_nm_id", None),
        "supplier_article": getattr(o, "supplier_article", None),
        "tech_size": getattr(o, "tech_size", None),
        "date": getattr(o, "date", None).isoformat() if getattr(o, "date", None) else None,
        "amount_rub": getattr(o, "amount_rub", None),
        "sum": _pick_order_sum(o),
    }

def _review_to_dict(r: Optional[Review]) -> Dict[str, Any]:
    if not r:
        return {}
    return {
        "id": getattr(r, "id", None),
        "review_ext_id": getattr(r, "review_ext_id", None),
        "rating": getattr(r, "rating", None),
        "created_at": getattr(r, "created_at", None).isoformat() if getattr(r, "created_at", None) else None,
        "user_name": getattr(r, "user_name", None),
        "order_id": getattr(r, "order_id", None),
        "last_order_shk_id": getattr(r, "last_order_shk_id", None),
    }

# ---------- HOWTO helpers (file_id кэш + автоаплоад) ----------
def _ensure_parent_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def _load_cached_howto_file_id() -> str:
    try:
        p = Path(HOWTO_VIDEO_CACHE_JSON)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            return str(data.get("file_id") or "")
    except Exception as e:
        logger.warning("HOWTO cache read failed: %r", e)
    return ""

def _save_cached_howto_file_id(fid: str) -> None:
    try:
        p = Path(HOWTO_VIDEO_CACHE_JSON)
        _ensure_parent_dir(p)
        p.write_text(json.dumps({"file_id": fid}, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("HOWTO video file_id cached at %s", p)
    except Exception as e:
        logger.warning("HOWTO cache write failed: %r", e)

def _get_howto_file_id() -> str:
    return (HOWTO_VIDEO_FILE_ID or "").strip() or _load_cached_howto_file_id()

async def _resolve_admin_chat_id(bot) -> Optional[int]:
    uname = HOWTO_VIDEO_ADMIN_USERNAME.lstrip("@").strip()
    if not uname:
        return None
    try:
        chat = await bot.get_chat(uname)
        return chat.id
    except Exception as e:
        logger.warning("Failed to resolve admin username '%s': %r", uname, e)
        return None

async def _ensure_howto_file_id(ctx_message: Message) -> Optional[str]:
    fid = _get_howto_file_id()
    if fid:
        return fid

    p = Path(HOWTO_VIDEO_PATH)
    if not p.is_file():
        logger.warning("HOWTO video file not found at %s", p)
        return None

    bot = ctx_message.bot
    try_targets: List[Tuple[str, int]] = []
    admin_chat_id = await _resolve_admin_chat_id(bot)
    if admin_chat_id:
        try_targets.append(("admin", admin_chat_id))
    try_targets.append(("current", ctx_message.chat.id))

    for label, chat_id in try_targets:
        try:
            sent = await bot.send_video(chat_id, FSInputFile(p), caption="HOWTO upload (one-time)")
            fid = sent.video.file_id if sent and sent.video else None
            if fid:
                _save_cached_howto_file_id(fid)
                logger.info("HOWTO uploaded to %s chat_id=%s, file_id=%s", label, chat_id, fid)
                return fid
        except Exception as e:
            logger.warning("HOWTO upload to %s chat_id=%s failed: %r", label, chat_id, e)

    return None

# ======== ТОЛЬКО ДЛЯ HOWTO: клавиатура с кнопкой «Назад»
def kb_howto_back_only():
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data=CB_BACK)
    kb.adjust(1)
    return kb.as_markup()

async def _send_howto_video(target: Message) -> None:
    try:
        fid = _get_howto_file_id()
        if not fid:
            fid = await _ensure_howto_file_id(target)

        if fid:
            await target.answer_video(fid, caption=HOWTO_CAPTION, reply_markup=kb_howto_back_only())
            return

        await target.answer(
            "Видео-инструкция временно недоступна. "
            "Проверьте, что instruction.mp4 лежит в корне проекта или задайте HOWTO_VIDEO_FILE_ID.",
            reply_markup=kb_howto_back_only(),
        )
    except Exception as e:
        logger.warning("Failed to send HOWTO video: %r", e)
        await target.answer("Не удалось отправить видео-инструкцию, попробуйте позже.", reply_markup=kb_howto_back_only())

# ---------- клавиатуры ----------
def kb_bonus_entry():
    kb = InlineKeyboardBuilder()
    kb.button(text="как сделать чек?", callback_data=CB_HOWTO)
    kb.button(text="в главное меню", callback_data=CB_TO_MAIN)
    kb.adjust(2, 1)
    return kb.as_markup()

def kb_no_order():
    kb = InlineKeyboardBuilder()
    kb.button(text="прислать другой чек", callback_data=CB_RETRY)
    kb.button(text="в главное меню", callback_data=CB_TO_MAIN)
    kb.adjust(2, 1)
    return kb.as_markup()

def kb_error():
    kb = InlineKeyboardBuilder()
    kb.button(text="отправить чек в службу поддержки", callback_data=CB_SUPPORT)
    kb.button(text="в главное меню", callback_data=CB_TO_MAIN)
    kb.adjust(1, 2)
    return kb.as_markup()

def kb_back_only():
    kb = InlineKeyboardBuilder()
    kb.button(text="в главное меню", callback_data=CB_TO_MAIN)
    kb.adjust(1)
    return kb.as_markup()

def kb_choose_bank():
    kb = InlineKeyboardBuilder()
    kb.button(text="сбербанк", callback_data=CB_BANK_SBER)
    kb.button(text="т-банк", callback_data=CB_BANK_T)
    kb.button(text="Альфа банк", callback_data=CB_BANK_ALFA)
    kb.button(text="в главное меню", callback_data=CB_TO_MAIN)
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def kb_to_main():
    kb = InlineKeyboardBuilder()
    kb.button(text="в главное меню", callback_data=CB_TO_MAIN)
    kb.adjust(1)
    return kb.as_markup()

def kb_bonus_choice():
    kb = InlineKeyboardBuilder()
    kb.button(text="Получить кэшбек за отзыв", callback_data=CB_REVIEW)
    kb.button(text="Получить кэшбек за репост", callback_data=CB_REPOST)
    kb.button(text="в главное меню", callback_data=CB_TO_MAIN)
    kb.adjust(1, 1, 2)
    return kb.as_markup()

def kb_submit_fail():
    kb = InlineKeyboardBuilder()
    kb.button(text="отправить чек повторно", callback_data=CB_RETRY)
    kb.button(text="в главное меню", callback_data=CB_TO_MAIN)
    kb.adjust(1, 2)
    return kb.as_markup()

# ---------- отправка подсказок ----------
async def send_bonus_entry(message: Message, state: FSMContext) -> None:
    # корректная установка состояния через FSMContext
    await state.set_state(BonusFSM.waiting_receipt)
    await message.answer(
        "Пришлите ссылку на чек или PDF-файл чека.",
        reply_markup=kb_bonus_entry(),
        disable_web_page_preview=True,
    )

# ---------- Google Sheets ----------
GSHEETS_SPREADSHEET_ID  = os.getenv("GSHEETS_SPREADSHEET_ID", "")
GSHEETS_SHEET_NAME      = os.getenv("GSHEETS_SHEET_NAME", "Заявки")
GSHEETS_CREDENTIALS_JSON = os.getenv("GSHEETS_CREDENTIALS_JSON", "")
GSHEETS_CREDENTIALS_B64  = os.getenv("GSHEETS_CREDENTIALS_B64", "")
PROJECT_ROOT             = os.getenv("PROJECT_ROOT", "")

try:
    import gspread  # type: ignore
    from google.oauth2.service_account import Credentials  # type: ignore
    _HAVE_GSHEETS = True
except Exception as _e:
    _HAVE_GSHEETS = False
    logger.info("gspread/google-auth not available: %r. Sheets write will be skipped.", _e)

def _resolve_cred_path(p: str) -> Optional[Path]:
    if not p:
        return None
    cand = Path(p)
    if cand.is_file():
        return cand
    if PROJECT_ROOT:
        cand2 = Path(PROJECT_ROOT) / p
        if cand2.is_file():
            return cand2
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        cand3 = parent / p
        if cand3.is_file():
            return cand3
    return None

def _load_gs_credentials():
    if not _HAVE_GSHEETS:
        return None
    if GSHEETS_CREDENTIALS_B64:
        try:
            data = json.loads(base64.b64decode(GSHEETS_CREDENTIALS_B64).decode("utf-8"))
            return Credentials.from_service_account_info(data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except Exception as e:
            logger.warning("Failed to parse GSHEETS_CREDENTIALS_B64: %r", e)
            return None
    path = _resolve_cred_path(GSHEETS_CREDENTIALS_JSON)
    if path and path.is_file():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("Using GSheets credentials file: %s", path)
            return Credentials.from_service_account_info(data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except Exception as e:
            logger.warning("Failed to load GSHEETS credentials from %s: %r", path, e)
            return None
    if GSHEETS_CREDENTIALS_JSON and not Path(GSHEETS_CREDENTIALS_JSON).exists():
        try:
            data = json.loads(GSHEETS_CREDENTIALS_JSON)
            logger.info("Using inline JSON from GSHEETS_CREDENTIALS_JSON env")
            return Credentials.from_service_account_info(data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        except Exception:
            pass
    logger.warning("GSHEETS credentials not provided or not found. Set GSHEETS_CREDENTIALS_JSON.")
    return None

async def _append_row_to_gsheets(row: List[Any]) -> bool:
    if not (_HAVE_GSHEETS and GSHEETS_SPREADSHEET_ID):
        logger.warning("GSHEETS not configured (libs or env). Skipping write.")
        return False
    creds = _load_gs_credentials()
    if creds is None:
        logger.warning("GSHEETS credentials not loaded. Skipping write.")
        return False

    def _val(v: Any):
        if v is None:
            return ""
        if isinstance(v, Decimal):
            try:
                return float(v)
            except Exception:
                return str(v)
        return v

    values = [_val(v) for v in row]

    def _append():
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GSHEETS_SPREADSHEET_ID)
        try:
            ws = sh.worksheet(GSHEETS_SHEET_NAME)
        except Exception:
            ws = sh.add_worksheet(GSHEETS_SHEET_NAME, rows=1000, cols=20)
        ws.append_row(values, value_input_option="USER_ENTERED", table_range="A1")
        return True

    try:
        return await asyncio.to_thread(_append)
    except Exception as e:
        logger.warning("GSHEETS append failed: %r", e)
        return False

# ---------- вспомогательные шаги ----------
async def save_srids_for_support(state: FSMContext, srids: Iterable[str]) -> None:
    try:
        uniq = [s for s in (srids or []) if s]
        if uniq:
            await state.update_data(bonus_support_ctx={"srids": list(dict.fromkeys(uniq))})
    except Exception:
        pass

async def save_receipt_payload_for_support(state: FSMContext, message: Message) -> None:
    try:
        data = await state.get_data()
        ctx = (data.get("bonus_support_ctx") or {}) if isinstance(data, dict) else {}
        if message.document and (message.document.mime_type == "application/pdf"):
            ctx["receipt"] = {
                "type": "document",
                "file_id": message.document.file_id,
                "file_name": message.document.file_name,
                "mime": message.document.mime_type,
                "caption": message.caption,
            }
        elif message.text:
            ctx["receipt"] = {"type": "text", "text": message.text}
        await state.update_data(bonus_support_ctx=ctx)
    except Exception:
        pass

async def _reply_check_result(message: Message, ok: bool, why: str | None = None, override_text: str | None = None) -> None:
    if ok:
        logger.info("Result: OK -> %s", POSITIVE_REPLY)
        await message.answer(POSITIVE_REPLY, reply_markup=kb_to_main())
        return
    if override_text == NO_ORDER_REPLY:
        logger.warning("Result: NO_ORDER -> %s ; reason=%s", NO_ORDER_REPLY, why or "n/a")
        await message.answer(NO_ORDER_REPLY, reply_markup=kb_no_order())
    else:
        logger.warning("Result: FAIL -> %s ; reason=%s", NEGATIVE_REPLY, why or "n/a")
        await message.answer(NEGATIVE_REPLY, reply_markup=kb_error())

def _order_is_valid(o: Order) -> bool:
    if getattr(o, "is_cancel", False):
        logger.debug("Order %s rejected: canceled", getattr(o, "srid", None)); return False
    if VALIDATE_ORDER_MAX_AGE_DAYS > 0 and getattr(o, "date", None):
        delta = datetime.now(timezone.utc) - o.date
        if delta.days > VALIDATE_ORDER_MAX_AGE_DAYS:
            logger.debug("Order %s rejected: too old (%s > %s)", getattr(o, "srid", None), delta.days, VALIDATE_ORDER_MAX_AGE_DAYS)
            return False
    return True

async def _show_loading(message: Message, text: str = "⏳ Загрузка данных, ожидайте…") -> Optional[Message]:
    try:
        return await message.answer(text)
    except Exception:
        return None

async def _delete_message_silent(msg: Optional[Message]) -> None:
    if not msg:
        return
    try:
        await msg.delete()
    except Exception:
        pass

async def _collect_good_pairs_for_srid(srid_raw: str) -> Tuple[bool, List[Dict[str, Any]]]:
    logger.info("Collect good pairs for SRID: %s", srid_raw)
    core = srid_core(srid_raw)
    if not core:
        return (False, [])

    async with async_session_maker() as session:
        candidates = [srid_raw, core]
        orders = await find_orders_by_srids_fuzzy(session, candidates)
        logger.info("Orders found: %s", len(orders))
        if not orders:
            return (False, [])

        valid = [o for o in orders if _order_is_valid(o)]
        logger.info("Valid orders: %s", len(valid))
        if not valid:
            return (True, [])

        order_ids = [o.id for o in valid]
        reviews = await find_reviews_for_orders(session, order_ids)
        logger.info("Reviews for valid orders: %s", len(reviews))
        if not reviews:
            return (True, [])

        valid_by_id = {o.id: o for o in valid}
        by_order: Dict[int, List[Review]] = {}
        for r in reviews:
            if r.order_id in valid_by_id and (getattr(r, "rating", 0) or 0) >= MIN_GOOD_RATING:
                by_order.setdefault(r.order_id, []).append(r)

        def dt_or_min(dt):
            return dt or datetime.min.replace(tzinfo=timezone.utc)

        good_pairs: List[Dict[str, Any]] = []
        for oid, rs in by_order.items():
            rs.sort(key=lambda r: dt_or_min(r.created_at), reverse=True)
            chosen_r = rs[0]
            chosen_o = valid_by_id[oid]
            good_pairs.append({"order": _order_to_dict(chosen_o), "review": _review_to_dict(chosen_r)})

        return (True, good_pairs)

def _aggregate_ok_details(all_pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    nmids: List[str] = []
    srids: List[str] = []
    review_ids: List[str] = []
    total_sum = Decimal("0")

    latest_dt: Optional[datetime] = None
    latest_dt_iso: Optional[str] = None

    for pr in all_pairs:
        o = pr.get("order", {}) or {}
        r = pr.get("review")  # may be {}
        nm = (o.get("nmId") or "") if o else ""
        sr = (o.get("srid") or "") if o else ""
        if nm:
            nmids.append(str(nm))
        if sr:
            srids.append(str(sr))
        if r and r.get("review_ext_id"):
            review_ids.append(str(r.get("review_ext_id")))

        sval = _to_dec(o.get("sum"))
        if sval:
            total_sum += sval

        if o.get("date"):
            try:
                d = datetime.fromisoformat(o["date"])
                if latest_dt is None or d > latest_dt:
                    latest_dt = d
                    latest_dt_iso = o["date"]
            except Exception:
                pass

    return {
        "nmids_str": "\n".join(_uniq_preserve(nmids)),
        "srids_str": "\n".join(_uniq_preserve(srids)),
        "review_ids_str": "\n".join(_uniq_preserve(review_ids)),
        "total_amount": total_sum.quantize(Decimal("0.01")),
        "date_latest": latest_dt_iso,
        "pairs": all_pairs,
    }

async def _start_payout_flow(message: Message, state: FSMContext, ok_details: Dict[str, Any]) -> None:
    await state.set_state(BonusFSM.waiting_phone)
    await state.update_data(bonus={
        "ok_details": ok_details,
        "phone": None,
        "bank": None,
        "submitting": False,
        "bank_msg_id": None,
    })
    await message.answer(
        "Введите номер телефона для получения кэшбека",
        reply_markup=kb_back_only()
    )

PHONE_RE = re.compile(r"^\+?\d[\d\-\s()]{6,}$")

async def _on_phone_received(message: Message, state: FSMContext, phone_text: str) -> None:
    phone = (phone_text or "").strip()
    norm_raw = re.sub(r"[^\d+]", "", phone)

    if not (norm_raw.startswith("+7") or norm_raw.startswith("8")):
        await message.answer(
            "Выплаты кэшбека доступны только на Российские номера.",
            reply_markup=kb_back_only()
        )
        return

    if not PHONE_RE.match(phone) and not PHONE_RE.match(norm_raw):
        await message.answer(
            "Похоже, номер телефона некорректный. Введите ещё раз (пример: +79991234567).",
            reply_markup=kb_back_only()
        )
        return

    digits = re.sub(r"\D", "", norm_raw)
    if norm_raw.startswith("8") and len(digits) == 11:
        norm = "+7" + digits[1:]
    elif norm_raw.startswith("+7") and len(digits) == 11:
        norm = "+7" + digits[1:]
    else:
        norm = norm_raw

    data = await state.get_data()
    bonus = data.get("bonus", {}) or {}
    bonus["phone"] = norm
    if message.from_user:
        bonus["tg_user_id"] = message.from_user.id
        bonus["tg_username"] = message.from_user.username
    await state.update_data(bonus=bonus)

    await state.set_state(BonusFSM.waiting_bank)
    msg = await message.answer(
        "Выберите (или введите, если вашего банка нет в списке) банк для перевода кэшбека",
        reply_markup=kb_choose_bank()
    )
    bonus["bank_msg_id"] = msg.message_id
    await state.update_data(bonus=bonus)

async def _finalize_application(message: Message, state: FSMContext, bank_name: str, loading_msg: Optional[Message] = None) -> None:
    data = await state.get_data()
    bonus = data.get("bonus", {}) or {}

    if bonus.get("submitting"):
        return
    bonus["submitting"] = True
    await state.update_data(bonus=bonus)

    bonus["bank"] = (bank_name or "").strip()
    await state.update_data(bonus=bonus)

    ok_details = bonus.get("ok_details") or {}
    nmids_str      = ok_details.get("nmids_str")
    srids_str      = ok_details.get("srids_str")
    review_ids_str = ok_details.get("review_ids_str")
    total_amount   = ok_details.get("total_amount")
    date_latest    = ok_details.get("date_latest")

    user_id = bonus.get("tg_user_id") or (message.from_user.id if message.from_user else None)
    username = bonus.get("tg_username") or (message.from_user.username if message.from_user else None)

    # Пытаемся получить комментарий/скидку для пользователя
    discount_comment = ""
    if user_id:
        try:
            discount_comment = await get_user_discount(int(user_id))
        except Exception as e:
            logger.warning("Failed to get user discount for %s: %r", user_id, e)

    row = [
        date_latest,           # A
        srids_str,             # B
        nmids_str,             # C
        total_amount,          # D
        MIN_GOOD_RATING,       # E
        review_ids_str,        # F
        bonus.get("phone"),    # G
        bonus.get("bank"),     # H
        user_id,               # I
        username,              # J
        datetime.now(timezone.utc).isoformat(),  # K
        discount_comment,      # L (столбец с индексом 11)
    ]

    success = False
    try:
        success = await _append_row_to_gsheets(row)
    except Exception as e:
        logger.warning("GSHEETS append raised exception: %r", e)
        success = False

    logger.info("GSHEETS append %s", "OK" if success else "SKIPPED/FAILED")

    await _delete_message_silent(loading_msg)

    if success:
        if user_id and discount_comment:
            try:
                await set_user_discount(int(user_id), "")
            except Exception as e:
                logger.warning("Failed to clear discount comment for %s: %r", user_id, e)

        try:
            pairs = (ok_details.get("pairs") or [])
            srid_to_order_id: Dict[str, int] = {}

            for pr in pairs:
                od = pr.get("order") or {}
                sr = (od.get("srid") or "") if od else ""
                oid = od.get("id")
                if sr and (oid is not None):
                    s_norm = srid_core(str(sr)) or str(sr)
                    srid_to_order_id.setdefault(s_norm, int(oid))

            if srid_to_order_id:
                logger.info("bonus_claims upsert (strict): mapping=%s", srid_to_order_id)
                async with async_session_maker() as session:
                    await insert_claims_for_orders(
                        session,
                        srid_to_order_id,
                        tg_user_id=str(user_id) if user_id is not None else None,
                        tg_username=username,
                        phone=bonus.get("phone"),
                        bank=bonus.get("bank"),
                    )
            else:
                logger.info("bonus_claims upsert (strict): skipped — no SRID→order_id pairs")
        except Exception as e:
            logger.warning("failed to insert bonus claims: %r", e)

        try:
            data_after = await state.get_data()
            bonus_ctx = (data_after or {}).get("bonus") or {}
            tid = bonus_ctx.get("ticket_id")
            if tid:
                await set_status_and_render(message, tid, TStatus.CLOSED, reason="bonus_finished")
        except Exception:
            pass

        await state.clear()
        await message.answer(
            "Заявка на получение кэшбека создана, перевод пройдёт в ближайшие 7 дней",
            reply_markup=kb_to_main()
        )
    else:
        await state.clear()
        await message.answer("Произошла ошибка", reply_markup=kb_submit_fail())

# ---------- коллбеки и обработчики ----------
@router.callback_query(F.data == CB_HOWTO)
async def on_howto(cb: CallbackQuery):
    await cb.answer()
    await _send_howto_video(cb.message)

@router.callback_query(F.data == CB_BACK)
async def on_back(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        if getattr(cb.message, "caption", "") == HOWTO_CAPTION:
            try:
                await cb.message.delete()
            except Exception:
                pass
            # Важно: состояние НЕ трогаем → остаёмся ждать чек
            return
    except Exception:
        pass

    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.clear()
    await send_main_menu(cb.message)

# === Кнопка «отправить чек в службу поддержки»
@router.callback_query(F.data == CB_SUPPORT)
async def on_support(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(SupportStates.AwaitReviewProof)
    await cb.message.answer(
        "Пожалуйста, предоставьте скриншот оставленного вами отзыва (или другое подтверждение).",
        reply_markup=kb_back_only()
    )

@router.callback_query(F.data == CB_RETRY)
async def on_retry(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.clear()
    await send_bonus_entry(cb.message, state)

@router.callback_query(F.data.in_(CB_START_ALIASES))
async def on_bonus_start_any(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.clear()
    await cb.message.answer(
        "Выберите способ получения кэшбека:",
        reply_markup=kb_bonus_choice()
    )

@router.message(F.text.func(lambda t: t and t.strip().casefold() in GET_BONUS_TEXTS))
async def on_bonus_text_entry(message: Message, state: FSMContext):
    await state.clear()
    await send_bonus_entry(message, state)

@router.message(F.text.lower() == "/bonus")
async def on_bonus_cmd(message: Message, state: FSMContext):
    await state.clear()
    await send_bonus_entry(message, state)

# 🔁 Триггерим обработчик по ссылкам/SRID
@router.message(
    F.text.func(
        lambda t: bool(t) and (
            ("receipt.wb.ru" in t.lower()) or
            re.search(r"(?i)https?://", t) or
            re.search(_SRID_ANY_RE, t)
        )
    )
)
async def handle_receipt_text_or_link(message: Message, state: FSMContext) -> None:
    loading = await _show_loading(message)
    try:
        raw = (message.text or "").strip()
        logger.info("Incoming TEXT/URL message id=%s len=%s", message.message_id, len(raw))
        all_srids: List[str] = []

        direct = extract_srids_loose_from_text(raw)
        all_srids += direct

        urls = re.findall(r"(?i)((?:https?://)?[^\s]*receipt\.wb\.ru[^\s]*)", raw) or re.findall(r"(?i)https?://\S+", raw)
        for u in urls:
            url = u if "://" in u else f"https://{u}"
            try:
                all_srids += extract_srids_from_url(url)
            except Exception:
                pass
            try:
                all_srids += await extract_srids_from_url_async_all(url)
            except Exception:
                pass
            try:
                all_srids += await fetch_html_and_extract_srids(url)
            except Exception:
                pass

        uniq_srids = _uniq_preserve(all_srids)
        logger.info("SRIDs uniq collected: %s", len(uniq_srids))
        if not uniq_srids:
            await _delete_message_silent(loading)
            return await _reply_check_result(message, ok=False, why="srid not found in text/url", override_text=NO_ORDER_REPLY)

        try:
            srids_for_check = _expand_with_core(uniq_srids)
            async with async_session_maker() as session:
                claimed_early = await get_claimed_srids(session, srids_for_check)
            if claimed_early:
                await _delete_message_silent(loading)
                return await message.answer(ALREADY_CLAIMED_REPLY, reply_markup=kb_back_only())
        except Exception as e:
            logger.warning("early claimed check failed: %r", e)

        srids_to_process = uniq_srids[:BONUS_MAX_SRIDS or None]

        any_orders_found = False
        aggregated_pairs: List[Dict[str, Any]] = []

        for sr in srids_to_process:
            found, pairs = await _collect_good_pairs_for_srid(sr)
            any_orders_found = any_orders_found or found
            if pairs:
                aggregated_pairs.extend(pairs)

        await _delete_message_silent(loading)

        if aggregated_pairs:
            ok_details = _aggregate_ok_details(aggregated_pairs)

            srids_list = [p.get("order", {}).get("srid") for p in ok_details.get("pairs", []) if p.get("order")]
            srids_list = [s for s in srids_list if s]
            if srids_list:
                srids_for_check2 = _expand_with_core(srids_list)
                async with async_session_maker() as session:
                    claimed = await get_claimed_srids(session, srids_for_check2)
                if claimed:
                    return await message.answer(ALREADY_CLAIMED_REPLY, reply_markup=kb_back_only())

            return await _start_payout_flow(message, state, ok_details)

        if any_orders_found:
            try:
                await save_srids_for_support(state, srids_to_process)
                await save_receipt_payload_for_support(state, message)
            except Exception:
                pass
            return await _reply_check_result(message, ok=False, why="orders found but no 5-star reviews")

        return await _reply_check_result(message, ok=False, why="no orders at all", override_text=NO_ORDER_REPLY)

    except Exception as e:
        logger.exception("Exception in handle_receipt_text_or_link: %r", e)
        await _delete_message_silent(loading)
        return await _reply_check_result(message, ok=False, why=f"exception: {e!r}")

@router.message(F.document & (F.document.mime_type == "application/pdf"))
async def handle_receipt_pdf(message: Message, state: FSMContext) -> None:
    loading = await _show_loading(message)
    file = message.document
    logger.info("Incoming PDF id=%s name=%s size=%s", message.message_id, file.file_name, file.file_size)
    tmp_path = os.path.join(tempfile.gettempdir(), f"{file.file_unique_id}.pdf")
    try:
        try:
            await message.document.download(destination=tmp_path)
        except Exception:
            file_info = await message.bot.get_file(file.file_id)
            await message.bot.download_file(file_info.file_path, destination=tmp_path)
        try:
            best, all_cands, _ = extract_srid_from_pdf(tmp_path)
            logger.info("PDF parsed: best=%s ; candidates=%s", best, len(all_cands))
        except RuntimeError as e:
            await _delete_message_silent(loading)
            return await _reply_check_result(message, ok=False, why=f"pdf parse runtime: {e}")

        if not all_cands:
            await _delete_message_silent(loading)
            return await _reply_check_result(message, ok=False, why="no srid in pdf", override_text=NO_ORDER_REPLY)

        uniq_srids = _uniq_preserve(all_cands)

        try:
            srids_for_check = _expand_with_core(uniq_srids)
            async with async_session_maker() as session:
                claimed_early = await get_claimed_srids(session, srids_for_check)
            if claimed_early:
                await _delete_message_silent(loading)
                return await message.answer(ALREADY_CLAIMED_REPLY, reply_markup=kb_back_only())
        except Exception as e:
            logger.warning("early claimed check (pdf) failed: %r", e)

        srids_to_process = uniq_srids[:BONUS_MAX_SRIDS or None]

        any_orders_found = False
        aggregated_pairs: List[Dict[str, Any]] = []

        for sr in srids_to_process:
            found, pairs = await _collect_good_pairs_for_srid(sr)
            any_orders_found = any_orders_found or found
            if pairs:
                aggregated_pairs.extend(pairs)

        await _delete_message_silent(loading)

        if aggregated_pairs:
            ok_details = _aggregate_ok_details(aggregated_pairs)

            srids_list = [p.get("order", {}).get("srid") for p in ok_details.get("pairs", []) if p.get("order")]
            srids_list = [s for s in srids_list if s]
            if srids_list:
                srids_for_check2 = _expand_with_core(srids_list)
                async with async_session_maker() as session:
                    claimed = await get_claimed_srids(session, srids_for_check2)
                if claimed:
                    return await message.answer(ALREADY_CLAIMED_REPLY, reply_markup=kb_back_only())

            return await _start_payout_flow(message, state, ok_details)

        if any_orders_found:
            try:
                await save_srids_for_support(state, srids_to_process)
                await save_receipt_payload_for_support(state, message)
            except Exception:
                pass
            return await _reply_check_result(message, ok=False, why="orders found but no 5-star reviews")

        return await _reply_check_result(message, ok=False, why="no orders in pdf", override_text=NO_ORDER_REPLY)
    except Exception as e:
        logger.exception("Exception in handle_receipt_pdf: %r", e)
        await _delete_message_silent(loading)
        return await _reply_check_result(message, ok=False, why=f"exception: {e!r}")
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

# === НОВОЕ: фолбэк — текст без ссылок/SRID, пока ждём чек
@router.message(
    BonusFSM.waiting_receipt,
    F.text.func(
        lambda t: bool(t)
        and ("receipt.wb.ru" not in t.lower())
        and not re.search(r"(?i)https?://", t)
        and not re.search(_SRID_ANY_RE, t)
    )
)
async def receipt_waiting_plain_text_repeat(message: Message, state: FSMContext):
    await message.answer(
        "Похоже, это не чек.\n"
        "Пожалуйста, пришлите ссылку на чек или PDF-файл чека. Можно также отправить текст/скрин с SRID.",
        reply_markup=kb_bonus_entry(),
        disable_web_page_preview=True,
    )
    # остаёмся в BonusFSM.waiting_receipt

# === НОВОЕ: фолбэк — любые вложения, кроме PDF, пока ждём чек
@router.message(
    BonusFSM.waiting_receipt,
    F.content_type.in_({
        ContentType.PHOTO,
        ContentType.VIDEO,
        ContentType.ANIMATION,
        ContentType.VOICE,
        ContentType.AUDIO,
        ContentType.STICKER,
        ContentType.DOCUMENT,  # ниже исключим PDF
    })
)
async def receipt_waiting_media_repeat(message: Message, state: FSMContext):
    if message.document and (message.document.mime_type == "application/pdf"):
        return  # PDF поймает профильный обработчик
    await message.answer(
        "Это вложение я не могу обработать.\n"
        "Пожалуйста, пришлите ссылку на чек или PDF-файл чека.",
        reply_markup=kb_bonus_entry(),
        disable_web_page_preview=True,
    )
    # остаёмся в BonusFSM.waiting_receipt

@router.message(BonusFSM.waiting_phone)
async def on_phone_input(message: Message, state: FSMContext):
    await _on_phone_received(message, state, message.text or "")

@router.callback_query(F.data.in_(BANK_CB_SET), BonusFSM.waiting_bank)
async def on_bank_choice(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    bonus = data.get("bonus", {}) or {}
    if bonus.get("submitting"):
        await cb.answer("Заявка уже обрабатывается…")
        return

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    loading = await _show_loading(cb.message, "⏳ Создаём заявку, ожидайте…")
    mapping = {CB_BANK_SBER: "сбербанк", CB_BANK_T: "т-банк", CB_BANK_ALFA: "Альфа банк"}
    bank = mapping.get(cb.data, "банк")
    await cb.answer()
    await _finalize_application(cb.message, state, bank, loading_msg=loading)

@router.message(BonusFSM.waiting_bank)
async def on_bank_text(message: Message, state: FSMContext):
    data = await state.get_data()
    bonus = data.get("bonus", {}) or {}

    if bonus.get("submitting"):
        await message.answer("Заявка уже обрабатывается…", reply_markup=kb_back_only())
        return

    bank_msg_id = bonus.get("bank_msg_id")
    if bank_msg_id:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=bank_msg_id,
                reply_markup=None
            )
        except Exception:
            pass

    bank = (message.text or "").strip()
    if not bank:
        await message.answer("Введите название банка, либо выберите кнопку ниже.", reply_markup=kb_choose_bank())
        return

    loading = await _show_loading(message, "⏳ Создаём заявку, ожидайте…")
    await _finalize_application(message, state, bank, loading_msg=loading)

# === ЛЮБОЕ следующее сообщение после CB_SUPPORT → создаём тикет поддержки
@router.message(
    SupportStates.AwaitReviewProof,
    F.content_type.in_({ContentType.PHOTO, ContentType.DOCUMENT, ContentType.TEXT, ContentType.VIDEO, ContentType.ANIMATION})
)
async def support_create_ticket_from_bonus(msg: Message, state: FSMContext):
    data = await state.get_data()
    ctx  = (data.get("bonus_support_ctx") or {}) if isinstance(data, dict) else {}
    srids: List[str] = (ctx.get("srids") or []) if isinstance(ctx, dict) else []
    receipt_ctx: Dict[str, Any] = ctx.get("receipt") or {}

    ticket_id = f"BONUS-{secrets.token_hex(3).upper()}"
    title = f"{ticket_id} • @{msg.from_user.username}" if msg.from_user and msg.from_user.username else f"{ticket_id} • {msg.from_user.full_name}"

    thread_id = await create_forum_topic(msg, title)
    header = ticket_header(ticket_id, msg.from_user)
    if srids:
        header = f"{header}\n\nℹ️ SRID’ы из чека: {', '.join(srids)}"
    card_msg = await send_card_in_thread(msg, thread_id, header)

    BONUS_TICKET_CTX[ticket_id] = {
        "user_id": msg.from_user.id if msg.from_user else None,
        "thread_id": thread_id,
        "srids": srids,
        "source": "bonus_manual_review",
    }

    try:
        if receipt_ctx.get("type") == "text" and receipt_ctx.get("text"):
            await msg.bot.send_message(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=thread_id,
                text=f"📎 Исходный чек (текст/ссылка):\n{receipt_ctx['text']}",
                disable_web_page_preview=False,
            )
        elif receipt_ctx.get("type") == "document" and receipt_ctx.get("file_id"):
            await msg.bot.send_document(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=thread_id,
                document=receipt_ctx["file_id"],
                caption="📎 Исходный чек (PDF)",
            )
    except Exception:
        pass

    try:
        await post_user_payload_into_thread(msg, thread_id)
    except Exception:
        try:
            await msg.bot.send_message(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=thread_id,
                text="(не удалось переслать вложение автоматически, проверьте ЛС пользователя)",
            )
        except Exception:
            pass

    try:
        await insert_ticket(
            ticket_id=ticket_id,
            user_id=msg.from_user.id if msg.from_user else None,
            thread_id=thread_id,
            status=TStatus.OPEN,
            general_msg_id=None,
            card_msg_id=card_msg.message_id,
        )
    except Exception:
        pass

    try:
        await set_status_and_render(card_msg, ticket_id, TStatus.OPEN, reason=None)
    except Exception:
        pass

    await msg.answer(f"✅ Ваш чек передан в службу поддержки.\nНомер тикета: `#{ticket_id}`", parse_mode="Markdown", reply_markup=kb_to_main())
    await state.clear()

# ==== Кнопки саппорта: «кэшбек OK» и «кэшбек не подходит»
def _pick_latest_review_for_order(reviews: List[Review], order_id: int) -> Optional[Review]:
    rs = [r for r in (reviews or []) if getattr(r, "order_id", None) == order_id]
    if not rs:
        return None
    rs.sort(key=lambda r: getattr(r, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rs[0]

def _build_pairs_for_srids(orders: List[Order], reviews: List[Review]) -> List[Dict[str, Any]]:
    pairs: List[Dict[str, Any]] = []
    by_id = {getattr(o, "id", None): o for o in (orders or []) if getattr(o, "id", None) is not None}
    for oid, o in by_id.items():
        od = _order_to_dict(o)
        rv = _pick_latest_review_for_order(reviews, oid)
        rd = _review_to_dict(rv) if rv else {}
        pairs.append({"order": od, "review": rd})
    return pairs

class RepostFSM(StatesGroup):
    waiting_screenshot = State()

@router.callback_query(F.data == CB_REVIEW)
async def on_bonus_review(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.clear()
    await send_bonus_entry(cb.message, state)

@router.callback_query(F.data == CB_REPOST)
async def on_bonus_repost(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await state.set_state(RepostFSM.waiting_screenshot)
    await cb.message.answer("📸 Пришлите скриншот репоста из соц. сетей.", reply_markup=kb_back_only())

@router.message(RepostFSM.waiting_screenshot, F.content_type.in_({ContentType.PHOTO, ContentType.DOCUMENT, ContentType.TEXT}))
async def handle_repost_screenshot(message: Message, state: FSMContext):
    if message.content_type == ContentType.TEXT:
        await message.answer("Нужно прислать именно СКРИНШОТ репоста (как изображение).", reply_markup=kb_back_only())
        return

    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.answer("Нужно прислать изображение. Пожалуйста, пришлите скриншот репоста.", reply_markup=kb_back_only())
        return

    from wbshop_bot.support.forum import (
        create_forum_topic,
        send_card_in_thread,
        ticket_header,
        get_general_thread_id,
        register_bonus_ticket,
        SUPPORT_GROUP_ID,
        PROJECT_TAG,
    )
    from wbshop_bot.support.repo import insert_ticket

    ticket_id = f"BONUS-{secrets.token_hex(3).upper()}-RP"
    register_bonus_ticket(ticket_id)

    thread_id = await create_forum_topic(message, "REPOST")

    header = ticket_header(ticket_id, message.from_user)
    header = f"{header}\n\nТип: REPOST"
    card_msg = await send_card_in_thread(message, thread_id, header)

    try:
        if message.photo:
            await message.bot.send_photo(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=thread_id,
                photo=file_id,
                caption="📎 Скриншот репоста"
            )
        else:
            await message.bot.send_document(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=thread_id,
                document=file_id,
                caption="📎 Скриншот репоста"
            )
    except Exception:
        pass

    try:
        general_thread_id = await get_general_thread_id()
        if general_thread_id:
            await message.bot.send_message(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=general_thread_id,
                text=f"#{PROJECT_TAG} │ Тикет **{ticket_id}** (REPOST) создан",
                parse_mode="Markdown"
            )
    except Exception:
        pass

    await insert_ticket(
        ticket_id=ticket_id,
        user_id=message.from_user.id,
        thread_id=thread_id,
        status="OPEN",
        general_msg_id=None,
        card_msg_id=card_msg.message_id
    )

    try:
        BONUS_TICKET_CTX[ticket_id] = {
            "user_id": message.from_user.id,
            "thread_id": thread_id,
            "srids": [],
            "source": "repost",
        }
    except Exception:
        pass

    await message.answer("✅ Ваше обращение отправлено и будет обработано в ближайшее время.", reply_markup=kb_to_main())
    await state.clear()

@router.callback_query(F.data.startswith("bonus:ok:"))
async def on_bonus_ok(cb: CallbackQuery, state: FSMContext):
    try:
        _, _, ticket_id = cb.data.split(":")
    except Exception:
        return await cb.answer("Некорректный callback", show_alert=True)

    ctx = BONUS_TICKET_CTX.get(ticket_id) or {}
    user_id = ctx.get("user_id")
    srids: List[str] = ctx.get("srids") or []
    if not user_id:
        return await cb.answer("Нет данных о пользователе", show_alert=True)

    try:
        await set_status_and_render(cb.message, ticket_id, TStatus.PENDING_USER, reason=None)
    except Exception:
        pass

    ok_details: Dict[str, Any] = {"pairs": []}
    try:
        async with async_session_maker() as session:
            orders = await find_orders_by_srids_fuzzy(session, srids) if srids else []
            order_ids = [o.id for o in orders]
            reviews = await find_reviews_for_orders(session, order_ids) if order_ids else []
        pairs = _build_pairs_for_srids(orders, reviews) if orders else []
        ok_details = _aggregate_ok_details(pairs) if pairs else {"pairs": []}
    except Exception:
        pass

    storage = state.storage
    key = StorageKey(bot_id=cb.message.bot.id, chat_id=user_id, user_id=user_id)
    await storage.set_state(key, BonusFSM.waiting_phone.state)
    await storage.set_data(key, {"bonus": {
        "ok_details": ok_details, "phone": None, "bank": None, "submitting": False, "bank_msg_id": None,
        "ticket_id": ticket_id, "thread_id": ctx.get("thread_id"),
    }})

    try:
        await cb.message.bot.send_message(
            chat_id=user_id,
            text="Заказ найден, отзыв проверен. Введите номер телефона для получения кэшбека",
            reply_markup=kb_back_only()
        )
    except Exception:
        pass
    await cb.answer("Запрос на телефон отправлен пользователю")

@router.callback_query(F.data.startswith("bonus:no:"))
async def on_bonus_no(cb: CallbackQuery, state: FSMContext):
    try:
        _, _, ticket_id = cb.data.split(":")
    except Exception:
        return await cb.answer("Некорректный callback", show_alert=True)

    ctx = BONUS_TICKET_CTX.get(ticket_id) or {}
    user_id = ctx.get("user_id")
    srids: List[str] = ctx.get("srids") or []

    try:
        await set_status_and_render(cb.message, ticket_id, TStatus.CLOSED, reason="bonus_denied")
    except Exception:
        pass

    try:
        if srids:
            async with async_session_maker() as session:
                orders = await find_orders_by_srids_fuzzy(session, srids)
                mapping: Dict[str, int] = {}
                for o in (orders or []):
                    sr = getattr(o, "srid", None)
                    oid = getattr(o, "id", None)
                    if sr and (oid is not None):
                        s_norm = srid_core(str(sr)) or str(sr)
                        mapping.setdefault(s_norm, int(oid))
                if mapping:
                    await insert_claims_for_orders(
                        session,
                        mapping,
                        tg_user_id=str(user_id) if user_id else None,
                        tg_username=cb.from_user.username
                    )
                else:
                    logger.info("bonus:no strict insert skipped — no SRID→order_id pairs for %s", srids)
    except Exception as e:
        logger.warning("bonus:no strict insert failed: %r", e)

    await cb.answer("Тикет закрыт. Чек(и) помечены как обработанные (если были найдены соответствующие заказы).")

@router.callback_query(F.data == CB_TO_MAIN)
async def on_to_main(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await send_main_menu(cb.message)
