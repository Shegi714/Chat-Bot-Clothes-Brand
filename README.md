## Chat_Bot_Clothes_Brand (public demo)

Публичная, обезличенная **демо-версия Telegram-бота** (aiogram) для бренда **Example Brand** — сделана как портфолио-проект: без персональных данных, боевых токенов и приватных ссылок. Конфигурация и интеграции вынесены в переменные окружения.

### Summary
- **Domain**: customer support + cashback flow + notifications for a clothes brand (demo)
- **Focus**: architecture, integrations, data handling, and publish-safe repo hygiene

### Features
- **Menu + FAQ**: контентные разделы, включая демо-юридические тексты
- **Support tickets (forum topics)**: тикеты в Telegram-супергруппе с форумом (опционально)
- **Cashback flow**: приём чека/ссылки/PDF, поиск/валидации, (опционально) запись заявки в Google Sheets
- **Notifications**: пересылка постов из канала по хэштегам (опционально)

### Tech stack
- **Python 3**
- **aiogram** (Telegram bot framework)
- **SQLAlchemy + aiosqlite** (storage)
- **python-dotenv** (local env)
- **pdfminer.six** (PDF parsing)

### Run locally
Перейдите в папку `wbshop-bot/`, установите зависимости и запустите бота:

```bash
pip install -r requirements.txt
cp .env.example .env
python main.py
```

### Project layout
Код собран в пакет `wbshop_bot/`, а `main.py` остаётся entrypoint’ом.

```text
wbshop-bot/
  main.py                 # entrypoint
  requirements.txt
  .env.example
  .env.full.example
  scripts/                # утилиты/скрипты запуска
  wbshop_bot/             # основной пакет
    agents/               # фоновые задачи (orders/reviews)
    integrations/         # внешние API (WB)
    services/             # сервисные хелперы (например receipts parser)
    storage/              # БД, модели, DAO, токены
    support/              # тикеты/поддержка (forum + repo)
    ui/                   # роутеры меню/FAQ/notify/partner
    cashback.py           # кэшбек-флоу (router)
    config.py             # env-driven конфиг
```

### Configuration
- **Minimal**: `wbshop-bot/.env.example`
- **Full reference**: `wbshop-bot/.env.full.example`
- **Runtime config loader**: `wbshop-bot/config.py`

Основные переменные:
- **`BOT_TOKEN`**: токен Telegram-бота (обязателен для запуска)
- **`BRAND_NAME`, `BRAND_TAG`**: нейтральный брендинг в UI/сообщениях
- **`COMMUNITY_URL`, `BRAND_SITE_URL`, `CATALOG_*_URL`, `PARTNER_FORM_URL`**: ссылки и кнопки меню
- **`SUPPORT_GROUP_ID`, `GENERAL_THREAD_ID`**: интеграция поддержки (опционально)
- **`NOTIFY_SOURCE_CHANNEL`**: источник уведомлений (опционально)

### Engineering highlights (portfolio)
- **Sanitized/public-safe repo**: исключены секреты и локальные артефакты (`.env`, `creds`, venv, `__pycache__`, БД/медиа)
- **Config isolation**: брендинг/ссылки вынесены в env через единый `config.py`
- **Graceful degradation**: интеграции (GSheets/support/notify) опциональны и не блокируют базовый запуск

### Notes about secrets
Если проект когда-либо содержал реальные токены в истории git, их нужно считать скомпрометированными и **ротировать** (Telegram/WB/Google и т.д.).

