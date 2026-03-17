# scripts/migrate_sqlite.py
import os
import sqlite3
from urllib.parse import urlparse
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env из корня проекта
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///app.db")

def _path_from_db_url(db_url: str) -> str:
    if not db_url.startswith("sqlite"):
        raise RuntimeError("Этот мигратор рассчитан только на SQLite")
    # sqlite+aiosqlite:///E:/path/to/file.db  или sqlite+aiosqlite:///app.db
    if "///" in db_url:
        return db_url.split("///", 1)[1]
    raise RuntimeError("Не удалось извлечь путь к файлу БД из DATABASE_URL")

DB_PATH = _path_from_db_url(DATABASE_URL)

# Если путь относительный, делаем его относительно корня проекта
if not os.path.isabs(DB_PATH):
    DB_PATH = str(Path(__file__).parent.parent / DB_PATH)

def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f'PRAGMA table_info("{table}")')
    cols = {row[1] for row in cur.fetchall()}  # row[1] — имя колонки
    return cols

def ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str):
    cols = table_columns(conn, table)
    if column not in cols:
        print(f'  + добавляю {table}.{column} {coltype}')
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {coltype}')

def main():
    print(f"[migrate] DATABASE_URL = {DATABASE_URL}")
    print(f"[migrate] DB_PATH      = {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        # --- orders: новые поля ---
        print("[migrate] Проверяю таблицу orders ...")
        ensure_column(conn, "orders", "sticker", "TEXT")
        ensure_column(conn, "orders", "product_nm_id", "TEXT")
        ensure_column(conn, "orders", "supplier_article", "TEXT")
        ensure_column(conn, "orders", "tech_size", "TEXT")
        # на всякий случай (если в ранних версиях не было)
        ensure_column(conn, "orders", "order_ext_id", "TEXT")
        ensure_column(conn, "orders", "amount_rub", "INTEGER")

        # --- reviews: все поля, которых может не быть ---
        print("[migrate] Проверяю таблицу reviews ...")
        ensure_column(conn, "reviews", "review_ext_id", "TEXT")
        ensure_column(conn, "reviews", "text", "TEXT")
        ensure_column(conn, "reviews", "pros", "TEXT")
        ensure_column(conn, "reviews", "cons", "TEXT")
        ensure_column(conn, "reviews", "rating", "INTEGER")
        ensure_column(conn, "reviews", "created_at", "DATETIME")
        ensure_column(conn, "reviews", "last_order_shk_id", "TEXT")
        ensure_column(conn, "reviews", "last_order_created_at", "DATETIME")
        ensure_column(conn, "reviews", "nm_id", "TEXT")
        ensure_column(conn, "reviews", "supplier_article", "TEXT")
        ensure_column(conn, "reviews", "matching_size", "TEXT")
        ensure_column(conn, "reviews", "user_name", "TEXT")
        ensure_column(conn, "reviews", "state", "TEXT")
        ensure_column(conn, "reviews", "was_viewed", "BOOLEAN")
        ensure_column(conn, "reviews", "created_row_at", "DATETIME")
        ensure_column(conn, "reviews", "order_id", "INTEGER")

        # --- sync_cursors: value уже есть, лишний раз не трогаем ---
        print("[migrate] Проверяю таблицу sync_cursors ...")
        ensure_column(conn, "sync_cursors", "value", "TEXT")

        conn.commit()
        print("[migrate] Готово. Схема приведена к актуальной версии.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()

