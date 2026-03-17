# secrets_util.py
from typing import Optional
try:
    from cryptography.fernet import Fernet  # type: ignore
except Exception:
    Fernet = None  # библиотека может отсутствовать

from wbshop_bot.config import TOKENS_FERNET_KEY

_KEY = TOKENS_FERNET_KEY  # сгенерируйте один раз: Fernet.generate_key().decode()

def enc(s: str) -> str:
    if not s:
        return s
    if not _KEY or not Fernet:
        return s
    return Fernet(_KEY).encrypt(s.encode("utf-8")).decode("utf-8")

def dec(s: str) -> str:
    if not s:
        return s
    if not _KEY or not Fernet:
        return s
    return Fernet(_KEY).decrypt(s.encode("utf-8")).decode("utf-8")
