# services/receipts.py
from __future__ import annotations
from typing import Optional, Tuple, List
from urllib.parse import urlparse, parse_qs, unquote
import re
import asyncio

from pdfminer.high_level import extract_text  # pip install pdfminer.six

# -------- Нормализация текста (точки/невидимые пробелы) --------
_DOT_CHARS = "·•∙․‧・．"  # нестандартные "точки" из PDF/HTML
_ZWS_CLASS = r"[\u00A0\u2007\u2008\u2009\u200A\u200B\u200C\u200D\u202F\u2060\uFEFF]"  # узкие/невидимые пробелы

def _pre_normalize_text(s: str) -> str:
    """Приводим текст к «дружелюбному» виду для регексов."""
    if not s:
        return s
    # заменяем все «нестандартные точки» на обычную
    s = s.translate({ord(ch): "." for ch in _DOT_CHARS})
    # удаляем невидимые пробелы
    s = re.sub(_ZWS_CLASS, "", s)
    # схлопываем обычные пробелы (кроме \n — часто не нужно)
    s = re.sub(r"[ \t\f\v]+", " ", s)
    return s

# -------- d[a-z0-9] кандидаты (приоритет №1) --------
# Ищем «d<буква|цифра>.» + непрерывный фрагмент до следующей точки (8..100 симв),
# затем ".x.y" (x,y — числа). После матча ядро чистим до [a-z0-9] и допускаем длину 24..64.
_RE_D_PREFIX_FULL_FLEX = re.compile(
    r"(?is)(?<![a-z0-9])"         # слева не буква/цифра
    r"(d[a-z0-9])\s*\.\s*"        # dX.
    r"([^.]{8,100})"              # ядро до следующей точки (буквы/цифры/пробелы)
    r"\s*\.\s*(\d+)\s*\.\s*(\d+)" # .x.y
    r"(?![a-z0-9])"               # справа не буква/цифра
)

# На случай отсутствия .x.y — ядро (используем реже, если вдруг нет суффиксов)
_RE_D_PREFIX_CORE_FLEX = re.compile(
    r"(?is)(?<![a-z0-9])"
    r"(d[a-z0-9])\s*\.\s*([^.]{8,100})"
    r"(?![a-z0-9])"
)

def _find_d_prefix_candidates(raw_text: str) -> List[str]:
    """Находит SRID вида d[a-z0-9].<ядро>[.x.y] с либеральным парсингом ядра."""
    s = _pre_normalize_text(raw_text)
    out: List[str] = []

    # Полные dX.<ядро>.x.y
    for m in _RE_D_PREFIX_FULL_FLEX.finditer(s):
        prefix, chunk, x, y = m.group(1), m.group(2), m.group(3), m.group(4)
        chunk_clean = re.sub(r"[^a-z0-9]", "", chunk.lower())  # важно: НЕ только [a-f0-9]
        if 24 <= len(chunk_clean) <= 64:
            out.append(f"{prefix.lower()}.{chunk_clean}.{x}.{y}")

    # Ядро, если не нашли полные
    if not out:
        cores = set()
        for m in _RE_D_PREFIX_CORE_FLEX.finditer(s):
            prefix, chunk = m.group(1), m.group(2)
            chunk_clean = re.sub(r"[^a-z0-9]", "", chunk.lower())
            if 24 <= len(chunk_clean) <= 64:
                core = f"{prefix.lower()}.{chunk_clean}"
                if core not in cores:
                    cores.add(core)
                    out.append(core)

    # Уникализируем, сохраняя порядок
    uniq, seen = [], set()
    for v in out:
        if v not in seen:
            uniq.append(v); seen.add(v)
    return uniq

# -------- Fallback: длинные числа (14–22) — только если dX.* не найден --------
_MIN_NUMERIC_SRID_LEN = 14

# хотим получить \d{14,22} -> пишем \d{{{_MIN_NUMERIC_SRID_LEN},22}}
_RE_NUMERIC = re.compile(rf"(?<!\d)(\d{{{_MIN_NUMERIC_SRID_LEN},22}})(?!\d)")
_RE_NUMERIC_WITH_SUFFIX = re.compile(
    rf"(?<!\d)(?P<num>\d{{{_MIN_NUMERIC_SRID_LEN},22}})\s*\.\s*\d+\s*\.\s*\d+(?!\d)"
)

# Маркеры рядом с реквизитами (ИНН/ККТ/ФН/ФД/ФПД и проч.), по которым длинные числа отбрасываем
_IGNORE_NUMERIC_MARKERS = (
    "инн", "огрн", "кпп", "бик",
    "ккт", "рн ккт", "рн", "рнм", "зн ккт", "зн", "офд",
    "фн", "фд", "фп", "фпд",
    "тел", "тел.", "тел:", "чек", "касс",
    "№", "no", "nr", "р/с", "рс", "сайт", "итого", "налог",
)

def _has_ignored_marker(text: str, start: int, end: int, window: int = 36) -> bool:
    """Проверяем контекст слева/справа на маркеры, означающие, что длинное число — не SRID."""
    left = text[max(0, start - window):start].lower()
    right = text[end:end + window].lower()
    ctx = (left + " " + right).replace("\xa0", " ").replace("\u202f", " ")
    return any(m in ctx for m in _IGNORE_NUMERIC_MARKERS)

def _find_numeric_candidates(raw_text: str) -> List[str]:
    s = _pre_normalize_text(raw_text)
    out: List[str] = []
    for m in _RE_NUMERIC_WITH_SUFFIX.finditer(s):
        if not _has_ignored_marker(s, m.start("num"), m.end("num")):
            out.append(m.group("num"))
    for m in _RE_NUMERIC.finditer(s):
        if not _has_ignored_marker(s, m.start(1), m.end(1)):
            out.append(m.group(1))
    uniq, seen = [], set()
    for v in out:
        if v not in seen:
            uniq.append(v); seen.add(v)
    return uniq

# -------- Общая выборка кандидатов --------
def _find_all_srids(raw_text: str) -> List[str]:
    """Возвращает список кандидатов SRID с приоритетом d[a-z0-9].* над числовыми."""
    d_pref = _find_d_prefix_candidates(raw_text)
    if d_pref:
        return d_pref  # если есть dX.* — длинные числа игнорируем полностью
    return _find_numeric_candidates(raw_text)

def _uniq_preserve(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def _find_all_srids_all(raw_text: str) -> List[str]:
    """
    Полный список кандидатов: сначала все d[a-z0-9].*, затем — длинные numeric.
    """
    d_pref = _find_d_prefix_candidates(raw_text)
    nums  = _find_numeric_candidates(raw_text)
    return _uniq_preserve(d_pref + [n for n in nums if n not in d_pref])

def _choose_best(cands: List[str]) -> Optional[str]:
    """Выбираем «лучший» SRID: полный dX -> ядро dX -> длинный numeric."""
    if not cands:
        return None
    for s in cands:
        if re.fullmatch(r"(?i)d[a-z0-9]\.[a-z0-9]{24,64}\.\d+\.\d+", s):
            return s
    for s in cands:
        if re.fullmatch(r"(?i)d[a-z0-9]\.[a-z0-9]{24,64}", s):
            return s
    for s in cands:
        if _RE_NUMERIC.fullmatch(s):
            return s
    return cands[0]

# -------- URL / домен чеков --------
_RE_HOST_RECEIPT = re.compile(r"\breceipt\.wb\.ru\b", re.IGNORECASE)

def extract_srids_from_text(text: str) -> List[str]:
    """Вернуть ВСЕ SRID из произвольной строки, в порядке приоритета."""
    if not text:
        return []
    return _find_all_srids_all(text)

def extract_srid_from_text(text: str) -> Optional[str]:
    """Вернуть лучший SRID из произвольной строки (для обратной совместимости)."""
    return _choose_best(extract_srids_from_text(text or ""))

def extract_srids_from_url(url: str) -> List[str]:
    """
    Вернуть ВСЕ SRID из URL (query/fragment/сам текст URL).
    Если это receipt.wb.ru — сначала парсим query/fragment приоритетно.
    """
    if not url:
        return []
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    cands: List[str] = []

    if _RE_HOST_RECEIPT.search(host):
        q = parse_qs(parsed.query or "")
        for key in ("srid", "SRID"):
            if key in q and q[key]:
                cands += _find_all_srids_all(unquote(q[key][0]))
        if parsed.fragment:
            cands += _find_all_srids_all(unquote(parsed.fragment))

    cands += _find_all_srids_all(unquote(url))
    return _uniq_preserve(cands)

def extract_srid_from_url(url: str) -> Optional[str]:
    """Лучший SRID из URL (обёртка для обратной совместимости)."""
    return _choose_best(extract_srids_from_url(url or ""))

async def extract_srids_from_url_async_all(url: str) -> List[str]:
    """
    Асинхронно подтягиваем HTML для receipt.wb.ru (если нужно) и вытягиваем ВСЕ SRID со страницы.
    Плюс всё, что нашли непосредственно в URL.
    """
    if not url:
        return []
    out = extract_srids_from_url(url)  # уже включает query/fragment/сам URL

    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc or parsed.path
    if not _RE_HOST_RECEIPT.search(host):
        return _uniq_preserve(out)

    fetch_url = parsed.geturl()

    def _fetch_text_sync(u: str) -> Optional[str]:
        try:
            import urllib.request
            req = urllib.request.Request(
                u,
                headers={
                    "User-Agent": "Mozilla/5.0 (WB-Bot SRID Parser)",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru,en;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except Exception:
            return None

    html = await asyncio.to_thread(_fetch_text_sync, fetch_url)
    if html:
        out += _find_all_srids_all(html)

    return _uniq_preserve(out)

async def extract_srid_from_url_async(url: str) -> Optional[str]:
    """Лучший SRID со страницы receipt.wb.ru (обратная совместимость)."""
    all_srids = await extract_srids_from_url_async_all(url)
    return _choose_best(all_srids)

# -------- PDF --------
def extract_srid_from_pdf(path: str) -> Tuple[Optional[str], List[str], str]:
    """
    Возвращает: (best_srid, all_candidates, text_excerpt)
    Теперь all_candidates = d[a-z0-9].* + numeric, в приоритетном порядке.
    """
    text = extract_text(path) or ""
    cands = _find_all_srids_all(text)
    best = _choose_best(cands)
    return best, cands, (text[:4000] if text else "")
