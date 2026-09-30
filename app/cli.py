"""Командная строка TaskFlow Server.

    python -m app.cli serve        запустить сервер
    python -m app.cli initdb       создать схему и начальные данные
    python -m app.cli admin        создать или сбросить пароль администратора
    python -m app.cli backup       создать резервную копию
    python -m app.cli warmup       скачать и загрузить модель Whisper
    python -m app.cli check        проверить окружение и доступность зависимостей
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from app.config import get_settings


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
        # За обратным прокси (nginx, Cloudflare) нужно доверять заголовкам,
        # иначе в журнале будет адрес прокси вместо реального клиента.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def cmd_initdb(_args: argparse.Namespace) -> int:
    from app.database import Base, get_engine
    from app.services.seed import seed_all

    async def run() -> None:
        from app.database import get_sessionmaker

        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with get_sessionmaker()() as session:
            await seed_all(session)
            await session.commit()
        print("Схема базы данных готова, начальные данные записаны.")

    asyncio.run(run())
    return 0


def cmd_admin(args: argparse.Namespace) -> int:
    from app.database import get_sessionmaker
    from app.security import hash_password

    async def run() -> None:
        from sqlalchemy import select

        from app.models import Role, User

        async with get_sessionmaker()() as session:
            admin_role = await session.scalar(select(Role).where(Role.key == "Администратор"))

            if args.username:
                user = await session.scalar(
                    select(User).where(User.username == args.username)
                )
                if user is None:
                    print(f"Пользователь {args.username} не найден.")
                    return
            else:
                count = 0
                async with get_sessionmaker()() as probe:
                    count = len((await probe.scalars(select(User))).all())
                if count:
                    print(
                        "В системе есть пользователи, поэтому новый администратор "
                        "не создаётся. Укажите имя существующего: --username"
                    )
                    return
                user = User(
                    username="admin",
                    full_name="Администратор",
                    password_hash=hash_password(args.password or "ChangeMe123"),
                    is_superuser=True,
                    is_active=True,
                    must_change_password=True,
                )
                if admin_role is not None:
                    user.roles.append(admin_role)
                session.add(user)
                await session.commit()
                print("Администратор создан: admin / (пароль из аргумента)")
                return

            password = args.password or getpass.getpass("Новый пароль: ")
            if len(password) < 8:
                print("Пароль должен быть не короче 8 символов.")
                return
            user.password_hash = hash_password(password)
            user.must_change_password = True
            user.is_active = True
            user.failed_login_count = 0
            user.locked_until = None
            await session.commit()
            print(f"Пароль пользователя {user.username} обновлён.")

    asyncio.run(run())
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from app.services.backup import create_backup

    result = asyncio.run(create_backup(include_files=not args.no_files))
    print(
        f"Копия создана: {result['filename']} "
        f"({result['size_bytes']} байт, файлов: {result['files']})"
    )
    return 0


def cmd_warmup(_args: argparse.Namespace) -> int:
    from app.services.voice_engine import engine

    print("Загружаем модель распознавания. Это может занять несколько минут...")
    asyncio.run(engine.warmup())
    print("Модель готова:", engine.model_info())
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    """Проверка окружения: помогает понять, что мешает запуску."""
    settings = get_settings()
    problems: list[str] = []

    print("Проверка окружения TaskFlow")
    print(f"  Python           : {sys.version.split()[0]}")
    print(f"  База данных      : {settings.database_url.split('://')[0]}")

    if settings.is_sqlite:
        db_path = Path(settings.path("./data")) / "taskflow.db"
        writable = db_path.parent.exists() and _writable(db_path.parent)
        print(f"  Файл базы       : {db_path} {'(каталог доступен)' if writable else '(НЕТ ДОСТУПА)'}")
        if not writable:
            problems.append(f"Нет прав на каталог {db_path.parent}")

    for label, path in (
        ("Файлы", settings.storage_path),
        ("Модели", settings.models_path),
        ("Бэкапы", settings.backups_path),
    ):
        print(f"  {label:16} : {path} {'(есть)' if path.exists() else '(будет создан)'}")

    print(f"  Распознавание   : {'включено' if settings.voice_enabled else 'выключено'}, "
          f"модель {settings.voice_model}")

    for module, hint in (
        ("faster_whisper", "pip install faster-whisper"),
        ("av", "pip install av"),
        ("argon2", "pip install argon2-cffi"),
        ("jwt", "pip install PyJWT"),
    ):
        try:
            __import__(module)
            print(f"  Пакет {module:16}: установлен")
        except ImportError:
            print(f"  Пакет {module:16}: НЕ УСТАНОВЛЕН — {hint}")
            if module in ("faster_whisper", "av") and settings.voice_enabled:
                problems.append(f"Нет пакета {module}, распознавание работать не будет")

    if settings.jwt_secret:
        print("  Ключ JWT         : задан в настройках")
    else:
        secret_file = settings.path("./data/secret.key")
        state = "будет создан при первом запуске"
        if secret_file.exists():
            state = "хранится в data/secret.key"
        print(f"  Ключ JWT         : {state}")

    print()
    if problems:
        print("Найдены проблемы:")
        for item in problems:
            print(f"  - {item}")
        return 1

    print("Всё в порядке. Запуск: python -m app.cli serve")
    return 0


def _writable(path: Path) -> bool:
    probe = path / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taskflow-server",
        description="Сервер TaskFlow: задачи, сотрудники, чат с распознаванием голоса",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="запустить сервер")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="перезагрузка при изменениях")
    serve.set_defaults(func=cmd_serve)

    initdb = sub.add_parser("initdb", help="создать схему и начальные данные")
    initdb.set_defaults(func=cmd_initdb)

    admin = sub.add_parser("admin", help="создать администратора или сбросить пароль")
    admin.add_argument("--username", default=None, help="существующий пользователь")
    admin.add_argument("--password", default=None, help="если не указан, спросит без эха")
    admin.set_defaults(func=cmd_admin)

    backup = sub.add_parser("backup", help="создать резервную копию")
    backup.add_argument("--no-files", action="store_true", help="без вложений")
    backup.set_defaults(func=cmd_backup)

    warmup = sub.add_parser("warmup", help="заранее загрузить модель Whisper")
    warmup.set_defaults(func=cmd_warmup)

    check = sub.add_parser("check", help="проверить окружение")
    check.set_defaults(func=cmd_check)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nОстановлено")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
