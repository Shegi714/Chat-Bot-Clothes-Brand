# manage_tokens.py
from __future__ import annotations
import argparse, asyncio
from sqlalchemy import text
from wbshop_bot.storage.db import engine
from wbshop_bot.storage.secrets_util import enc

async def add(alias: str, token: str):
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO wb_tokens (alias, token_enc, active)
            VALUES (:alias, :token_enc, 1)
            ON CONFLICT(alias) DO UPDATE SET token_enc=excluded.token_enc, active=1
        """), dict(alias=alias, token_enc=enc(token)))
    print(f"OK: added/updated {alias}")

async def enable(alias: str, value: bool):
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE wb_tokens SET active=:v WHERE alias=:a"),
                           dict(v=1 if value else 0, a=alias))
    print(f"OK: {'enabled' if value else 'disabled'} {alias}")

async def ls():
    async with engine.begin() as conn:
        r = await conn.execute(text("SELECT id, alias, active, added_at FROM wb_tokens ORDER BY id"))
        for row in r.mappings().all():
            print(f"{row['id']:>3}  {row['alias']:<20}  active={row['active']}  added_at={row['added_at']}")

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("--alias", required=True)
    p_add.add_argument("--token", required=True)
    p_en = sub.add_parser("enable")
    p_en.add_argument("--alias", required=True)
    p_en.add_argument("--on", action="store_true")
    p_en.add_argument("--off", action="store_true")
    sub.add_parser("list")

    args = p.parse_args()
    if args.cmd == "add":
        asyncio.run(add(args.alias, args.token))
    elif args.cmd == "enable":
        asyncio.run(enable(args.alias, value=bool(args.on and not args.off)))
    elif args.cmd == "list":
        asyncio.run(ls())

if __name__ == "__main__":
    main()
