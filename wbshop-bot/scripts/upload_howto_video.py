# scripts/upload_howto_video.py
import sys
from pathlib import Path

# Добавляем родительскую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import re
import json
import asyncio
from datetime import datetime

# опционально читаем .env
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

# ---------- конфиг из env ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# приоритет назначения:
HOWTO_VIDEO_TARGET_CHAT_ID = (os.getenv("HOWTO_VIDEO_TARGET_CHAT_ID", "") or "").strip()
HOWTO_VIDEO_THREAD_ID      = (os.getenv("HOWTO_VIDEO_THREAD_ID", "") or "").strip()
HOWTO_VIDEO_ADMIN_ID       = (os.getenv("HOWTO_VIDEO_ADMIN_ID", "") or "").strip()
HOWTO_VIDEO_ADMIN_USERNAME = (os.getenv("HOWTO_VIDEO_ADMIN_USERNAME", "") or "").strip().lstrip("@")

HOWTO_VIDEO_PATH       = (os.getenv("HOWTO_VIDEO_PATH", "instruction.mp4") or "").strip()
HOWTO_VIDEO_CACHE_JSON = (os.getenv("HOWTO_VIDEO_CACHE_JSON", ".howto_video_cache.json") or "").strip()

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан в .env")

def _parse_int(s: str) -> int | None:
    try:
        return int(s)
    except Exception:
        return None

def parse_chat_and_thread(ref: str) -> tuple[str | int, int | None] | None:
    """
    Поддерживаем форматы:
      -1001234567890
      -1001234567890_1   (или /1, или :1)
      @channelname
      @channelname/1     (или _1, :1)
    Возвращаем (chat_ref, thread_id_или_None), где chat_ref это int chat_id или '@name'.
    """
    if not ref:
        return None

    r = ref.strip()

    # Варианты с разделителем для thread_id
    m = re.fullmatch(r"@?(-?\d+)[_/:](\d+)", r)
    if m:
        chat_id = int(m.group(1))
        thread_id = int(m.group(2))
        return (chat_id, thread_id)

    m2 = re.fullmatch(r"@?([A-Za-z0-9_]{5,})[_/:](\d+)", r)
    if m2:
        chat_un = "@" + m2.group(1)
        thread_id = int(m2.group(2))
        return (chat_un, thread_id)

    # чисто числовой chat_id
    if r.lstrip("-").isdigit():
        return (int(r), None)

    # чистый username
    if r.startswith("@"):
        return (r, None)

    return None

async def resolve_target(bot: Bot) -> tuple[str | int, int | None, str]:
    """
    Возвращаем: (chat, thread_id, explanation_str_for_logs)
    Приоритет:
      1) HOWTO_VIDEO_TARGET_CHAT_ID (+ опц. HOWTO_VIDEO_THREAD_ID)
      2) HOWTO_VIDEO_ADMIN_ID
      3) HOWTO_VIDEO_ADMIN_USERNAME (только публичный канал/группа)
    """
    # 1) явный TARGET_CHAT_ID
    if HOWTO_VIDEO_TARGET_CHAT_ID:
        parsed = parse_chat_and_thread(HOWTO_VIDEO_TARGET_CHAT_ID)
        if parsed:
            chat_ref, th = parsed
        else:
            raise ValueError("❌ HOWTO_VIDEO_TARGET_CHAT_ID имеет непонятный формат")

        # если отдельно задан HOWTO_VIDEO_THREAD_ID — он имеет приоритет
        if HOWTO_VIDEO_THREAD_ID:
            th2 = _parse_int(HOWTO_VIDEO_THREAD_ID)
            if th2 is None:
                raise ValueError("❌ HOWTO_VIDEO_THREAD_ID должен быть числом")
            th = th2

        # если chat_ref — это '@username', проверим, что это публичный чат
        if isinstance(chat_ref, str) and chat_ref.startswith("@"):
            try:
                chat = await bot.get_chat(chat_ref)
                return (chat.id, th, f"target={chat.id}, thread={th} (по @{chat_ref})")
            except TelegramBadRequest as e:
                raise ValueError(
                    f"❌ Не удалось получить chat по {chat_ref}: {e}\n"
                    "Подсказка: для приватных чатов указывайте числовой chat_id."
                )
        return (chat_ref, th, f"target={chat_ref}, thread={th}")

    # 2) ADMIN_ID как приватный чат
    if HOWTO_VIDEO_ADMIN_ID:
        uid = _parse_int(HOWTO_VIDEO_ADMIN_ID)
        if uid is None:
            raise ValueError("❌ HOWTO_VIDEO_ADMIN_ID должен быть числовым user_id")
        # thread_id в ЛС не нужен
        return (uid, None, f"admin_id={uid}")

    # 3) ADMIN_USERNAME — только публичный канал/группа
    if HOWTO_VIDEO_ADMIN_USERNAME:
        handle = "@" + HOWTO_VIDEO_ADMIN_USERNAME
        try:
            chat = await bot.get_chat(handle)
            # можно дополнительно задать HOWTO_VIDEO_THREAD_ID (если это форум)
            th = None
            if HOWTO_VIDEO_THREAD_ID:
                th = _parse_int(HOWTO_VIDEO_THREAD_ID)
                if th is None:
                    raise ValueError("❌ HOWTO_VIDEO_THREAD_ID должен быть числом")
            return (chat.id, th, f"target={chat.id}, thread={th} (по @{HOWTO_VIDEO_ADMIN_USERNAME})")
        except TelegramBadRequest as e:
            raise ValueError(
                f"❌ Не удалось получить chat по {handle}: {e}\n"
                "Подсказка: для приватного пользователя используйте HOWTO_VIDEO_ADMIN_ID (числовой id), "
                "или укажите HOWTO_VIDEO_TARGET_CHAT_ID (id группы/канала, где бот состоит)."
            )

    raise ValueError(
        "❌ Не задан ни HOWTO_VIDEO_TARGET_CHAT_ID, ни HOWTO_VIDEO_ADMIN_ID, ни HOWTO_VIDEO_ADMIN_USERNAME.\n"
        "Укажите один из параметров назначения в .env."
    )

def save_cache(file_id: str, chat_id: str | int, path: Path) -> None:
    data = {
        "HOWTO_VIDEO_FILE_ID": file_id,
        "chat_id_used": chat_id,
        "saved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 Сохранено в {path}")
    except Exception as e:
        print(f"⚠️ Не удалось сохранить кэш {path}: {e}")

async def main() -> int:
    # Путь к видео относительно корня проекта
    video_path = Path(__file__).parent.parent / HOWTO_VIDEO_PATH
    if not video_path.is_file():
        # пробуем рядом со скриптом
        alt = Path(__file__).parent.parent / "instruction.mp4"
        if alt.is_file():
            video_path = alt
        else:
            print(f"❌ Файл видео не найден: {video_path}")
            return 2

    bot = Bot(BOT_TOKEN)
    try:
        try:
            target_chat, thread_id, expl = await resolve_target(bot)
        except ValueError as e:
            print(str(e))
            return 2

        print(f"➡️  Отправляем {video_path.name} в {expl}")
        kwargs = {}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id

        try:
            msg = await bot.send_video(chat_id=target_chat, video=FSInputFile(video_path), caption="HOWTO upload", **kwargs)
        except TelegramBadRequest as e:
            print(f"❌ Ошибка отправки видео: {e}")
            return 2
        except Exception as e:
            print(f"❌ Не удалось отправить видео: {e!r}")
            return 2

        if not msg or not getattr(msg, "video", None):
            print("❌ Ответ Telegram без video: не удалось получить file_id")
            return 2

        fid = msg.video.file_id
        print("\n✅ Успешно.")
        print("file_id:\n", fid)
        print("\n➡️  Добавьте в .env строку:")
        print(f"HOWTO_VIDEO_FILE_ID={fid}\n")

        cache_path = Path(__file__).parent.parent / HOWTO_VIDEO_CACHE_JSON
        save_cache(fid, msg.chat.id, cache_path)
        return 0

    finally:
        # гарантированно закрываем сессию
        try:
            await bot.session.close()
        except Exception:
            pass

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

