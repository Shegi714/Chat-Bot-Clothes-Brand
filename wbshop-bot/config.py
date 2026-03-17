import os

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None


# Ensure `.env` is loaded before reading any config constants.
# This keeps imports clean (no need to import config after calling load_dotenv elsewhere).
if load_dotenv:
    try:
        load_dotenv()
    except Exception:
        pass


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None:
        return default
    v = str(v).strip()
    return v if v != "" else default


def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


# --- Demo branding / content (public-safe defaults) ---
BRAND_NAME = _env("BRAND_NAME", "Example Brand")
BRAND_TAG = _env("BRAND_TAG", "EXAMPLE")  # used as a #tag in support/general messages
BOT_FALLBACK_USERNAME = _env("BOT_FALLBACK_USERNAME", "ExampleBrandBot")

BRAND_SITE_URL = _env("BRAND_SITE_URL", "https://example.com")
COMMUNITY_URL = _env("COMMUNITY_URL", "https://t.me/example_brand")

CATALOG_WB_URL = _env("CATALOG_WB_URL", "https://example.com/catalog/wildberries")
CATALOG_OZON_URL = _env("CATALOG_OZON_URL", "https://example.com/catalog/ozon")
CATALOG_YM_URL = _env("CATALOG_YM_URL", "https://example.com/catalog/yandex-market")

PARTNER_FORM_URL = _env("PARTNER_FORM_URL", "https://example.com/partner-form")

# --- Notifications channel (optional) ---
NOTIFY_SOURCE_CHANNEL = _env("NOTIFY_SOURCE_CHANNEL", "@example_brand_news")

# --- Integrations / secrets (must be set via env in real deployment) ---
BOT_TOKEN = _env("BOT_TOKEN", "")
WB_API_KEY = _env("WB_API_KEY", "")
TOKENS_FERNET_KEY = _env("TOKENS_FERNET_KEY", "")

# --- Support forum integration (optional in demo) ---
SUPPORT_GROUP_ID = _env_int("SUPPORT_GROUP_ID", 0)  # Telegram supergroup id (usually negative)
GENERAL_THREAD_ID = _env_int("GENERAL_THREAD_ID", 0)  # thread id inside forum (0 disables)
