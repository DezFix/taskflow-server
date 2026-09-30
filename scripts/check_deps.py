"""Сверяет реальные импорты приложения с зависимостями из pyproject.toml.

Нужен, потому что отсутствующая зависимость не мешает разработке, если
пакет уже стоял в окружении руками: сборка в Docker и в CI падает уже
после того, как проект опубликован.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(r"D:\CODE\Task-work\taskflow-server")

# Имя пакета в pip -> имена модулей в import.
SPECIAL = {
    "python-multipart": ["multipart"],
    "PyJWT": ["jwt"],
    "python-slugify": ["slugify"],
    "faster-whisper": ["faster_whisper"],
    "PyAV": ["av"],
    "email-validator": ["email_validator"],
    "pydantic-settings": ["pydantic_settings"],
    "argon2-cffi": ["argon2"],
    # Приезжают вместе с fastapi, отдельного объявления не требуют.
    "fastapi": ["starlette"],
    # Объявлен как необязательное дополнение previews.
    "pillow": ["PIL"],
}



# Модули, которые поставляет сам интерпретатор или фреймворк.
STDLIB = {
    "__future__", "abc", "argparse", "array", "ast", "asyncio", "base64",
    "binascii", "collections", "concurrent", "contextlib", "csv", "dataclasses",
    "datetime", "enum", "functools", "getpass", "gzip", "hashlib", "hmac", "io",
    "ipaddress", "itertools", "json", "logging", "math", "mimetypes", "os",
    "pathlib", "platform", "re", "secrets", "shutil", "socket", "sqlite3",
    "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading",
    "time", "tomllib", "traceback", "typing", "unicodedata", "urllib", "uuid",
    "warnings", "wave", "zipfile",
}


# Внутренние пакеты проекта.
LOCAL_PREFIXES = ("app", "tests", "scripts")


def collect_imports() -> set[str]:
    found: set[str] = set()
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "build", ".git", "data"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"  не разобран {path}: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                if node.module:
                    found.add(node.module.split(".")[0])
    return found


def declared_packages(data: dict) -> set[str]:
    """Достаёт имена пакетов, отбрасывая версии и extras.

    В pyproject зависимости записаны как "pydantic>=2.9" или
    "uvicorn[standard]>=0.32": для сравнения нужен только корень.
    """
    names: set[str] = set()
    groups = [data["project"]["dependencies"]]
    for extra in data["project"].get("optional-dependencies", {}).values():
        groups.append(extra)

    for group in groups:
        for entry in group:
            name = re.split(r"[<>=!~\[\s;]", entry, maxsplit=1)[0].strip()
            if name:
                names.add(name)
    return names


def main() -> int:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = declared_packages(data)

    modules: set[str] = set()
    for name in declared:
        modules.add(name.lower().replace("-", "_"))
        modules.update(SPECIAL.get(name, []))
    modules = {m for m in modules if m}


    imports = collect_imports()

    missing = []
    for name in sorted(imports):
        if name in STDLIB or name in LOCAL_PREFIXES:
            continue
        if name in modules:
            continue
        # Сторонний пакет может быть частью нашей же поставки.
        missing.append(name)

    if missing:
        print("Зависимости не объявлены в pyproject.toml:")
        for name in missing:
            print(f"  - {name}")
        return 1

    print(f"Все импорты покрыты объявленными зависимостями ({len(imports)} модулей)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
