"""Резервные копии и обслуживание хранилища."""

from __future__ import annotations

import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.database import get_engine
from app.errors import not_found
from app.logging_setup import get_logger
from app.security import new_id

logger = get_logger("backup")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


async def _dump_sqlite(target: Path) -> None:
    """Консистентная копия SQLite. Копирование файла на живом сервере
    может поймать файл в середине WAL-транзакции, поэтому сначала
    снимаем чекпоинт."""
    from sqlalchemy import text

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    source = get_engine().url.database
    if not source:
        raise RuntimeError("Не удалось определить путь к файлу базы")
    shutil.copy2(source, target)


async def create_backup(include_files: bool = True) -> dict:
    """Создаёт архив: база данных + файлы пользователей."""
    settings = get_settings()
    backup_dir = settings.backups_path
    backup_dir.mkdir(parents=True, exist_ok=True)

    archive_path = backup_dir / f"taskflow-{_stamp()}.zip"
    counters = {"database": 0, "files": 0, "bytes": 0}

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        if settings.is_sqlite:
            db_copy = backup_dir / f"_db-{new_id()[:8]}.db"
            try:
                await _dump_sqlite(db_copy)
                archive.write(db_copy, "database/taskflow.db")
                counters["database"] = 1
                counters["bytes"] += db_copy.stat().st_size
            finally:
                db_copy.unlink(missing_ok=True)
        else:
            # Для PostgreSQL/MySQL логический дамп делает внешняя команда;
            # в этом случае кладём в архив инструкцию.
            archive.writestr(
                "database/README.txt",
                "Для внешней СУБД снимите дамп командой поставщика "
                "и положите его в этот каталог архива.",
            )
        if include_files and settings.storage_path.exists():
            for path in settings.storage_path.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(settings.storage_path)
                    archive.write(path, f"files/{relative}")
                    counters["files"] += 1
                    counters["bytes"] += path.stat().st_size

    logger.info("backup_created", path=str(archive_path), **counters)
    return {
        "id": archive_path.stem,
        "filename": archive_path.name,
        "size_bytes": archive_path.stat().st_size,
        "created_at": archive_path.stat().st_mtime,
        "files": counters["files"],
        "includes_database": bool(counters["database"]),
    }


def list_backups() -> list[dict]:
    settings = get_settings()
    if not settings.backups_path.exists():
        return []
    result = []
    for path in sorted(settings.backups_path.glob("taskflow-*.zip"), reverse=True):
        stat = path.stat()
        result.append(
            {
                # Идентификатор — имя файла целиком, включая префикс:
                # backup_path склеивает его обратно без потерь.
                "id": path.stem,
                "filename": path.name,
                "size_bytes": stat.st_size,
                "created_at": stat.st_mtime,
            }
        )
    return result


def backup_path(backup_id: str) -> Path:
    settings = get_settings()
    safe = "".join(ch for ch in backup_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise not_found("backup_not_found", "Резервная копия не найдена")
    name = safe if safe.startswith("taskflow-") else f"taskflow-{safe}"
    path = settings.backups_path / f"{name}.zip"
    if not path.exists():
        raise not_found("backup_not_found", "Резервная копия не найдена")
    return path


def delete_backup(backup_id: str) -> None:
    path = backup_path(backup_id)
    path.unlink(missing_ok=True)
    logger.info("backup_deleted", backup_id=backup_id)


async def restore_backup(backup_id: str) -> dict:
    """Восстановление: файлы возвращаются, база — только если её заменили
    вручную и сервер перезапущен."""
    path = backup_path(backup_id)
    settings = get_settings()
    restored_files = 0
    restored_db = False

    with zipfile.ZipFile(path, "r") as archive:
        for member in archive.namelist():
            if member.startswith("files/"):
                relative = member[len("files/") :]
                # Путь из архива нельзя брать как есть: запись вида
                # files/../../windows/system32/... уехала бы писать
                # за пределы папки данных. Проверяем, что итоговый путь
                # остался внутри хранилища.
                target = (settings.storage_path / relative).resolve()
                root = settings.storage_path.resolve()
                if root not in target.parents and target != root:
                    logger.warning(
                        "backup_restore_skipped_unsafe_path",
                        backup_id=backup_id,
                        member=member,
                    )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                restored_files += 1
            elif member == "database/taskflow.db":
                restored_db = True

    logger.info("backup_restored", backup_id=backup_id, files=restored_files, db=restored_db)
    return {
        "backup_id": backup_id,
        "files_restored": restored_files,
        "database_found": restored_db,
        "note": (
            "Файлы восстановлены. База данных восстанавливается вручную: "
            "остановите сервер и замените data/taskflow.db файлом из архива."
            if restored_db
            else "В архиве нет файла базы данных — восстановлены только вложения."
        ),
    }


def cleanup_old_backups(keep: int = 10) -> int:
    """Оставляет последние keep копий, остальные удаляет."""
    backups = list_backups()
    removed = 0
    for item in backups[keep:]:
        try:
            backup_path(item["id"]).unlink(missing_ok=True)
            removed += 1
        except Exception:
            continue
    return removed
