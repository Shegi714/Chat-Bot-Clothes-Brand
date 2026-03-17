# scripts/dedup_reviews_sqlite.py
import sqlite3
from pathlib import Path

# Путь к БД относительно корня проекта
DB = Path(__file__).parent.parent / "app.db"

with sqlite3.connect(DB) as cx:
    cx.execute("PRAGMA foreign_keys=ON;")
    cx.executescript("""
    WITH d AS (
      SELECT id, review_ext_id,
             ROW_NUMBER() OVER (PARTITION BY review_ext_id ORDER BY id DESC) rn
      FROM reviews
    )
    DELETE FROM reviews WHERE id IN (SELECT id FROM d WHERE rn > 1);
    """)
    cx.execute("""
      CREATE UNIQUE INDEX IF NOT EXISTS uix_reviews_review_ext_id
      ON reviews (review_ext_id);
    """)
print("Done.")

