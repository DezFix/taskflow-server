"""Проверка созданной схемы БД: сколько таблиц и что в них есть."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "taskflow.db"


def main() -> int:
    if not DB.exists():
        print(f"Нет файла базы: {DB}")
        return 1

    con = sqlite3.connect(DB)
    try:
        tables = [
            row[0]
            for row in con.execute(
                "select name from sqlite_master where type='table' order by name"
            )
        ]
        print(f"База: {DB}")
        print(f"Таблиц: {len(tables)}")
        for name in tables:
            count = con.execute(f'select count(*) from "{name}"').fetchone()[0]
            print(f"  {name:20} строк: {count}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
