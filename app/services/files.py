"""Работа с файлами: приём, проверка, хранение, превью."""

from __future__ import annotations

import contextlib
import hashlib
import io
import mimetypes
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile

from app.config import get_settings
from app.errors import AppError, bad_request, not_found, too_large
from app.models import Attachment, AttachmentKind, User

#: Белый список расширений. В офисной сети этого достаточно,
#: а произвольный исполняемый файл принимать нельзя.
ALLOWED_EXTENSIONS: dict[str, AttachmentKind] = {
    # фото и скриншоты отчётов
    ".jpg": AttachmentKind.IMAGE,
    ".jpeg": AttachmentKind.IMAGE,
    ".png": AttachmentKind.IMAGE,
    ".webp": AttachmentKind.IMAGE,
    ".gif": AttachmentKind.IMAGE,
    ".bmp": AttachmentKind.IMAGE,
    ".heic": AttachmentKind.IMAGE,
    # документы
    ".pdf": AttachmentKind.DOCUMENT,
    ".doc": AttachmentKind.DOCUMENT,
    ".docx": AttachmentKind.DOCUMENT,
    ".xls": AttachmentKind.DOCUMENT,
    ".xlsx": AttachmentKind.DOCUMENT,
    ".csv": AttachmentKind.DOCUMENT,
    ".txt": AttachmentKind.DOCUMENT,
    ".log": AttachmentKind.DOCUMENT,
    ".md": AttachmentKind.DOCUMENT,
    ".zip": AttachmentKind.DOCUMENT,
    # аудио голосовых
    ".m4a": AttachmentKind.AUDIO,
    ".mp3": AttachmentKind.AUDIO,
    ".ogg": AttachmentKind.AUDIO,
    ".oga": AttachmentKind.AUDIO,
    ".opus": AttachmentKind.AUDIO,
    ".wav": AttachmentKind.AUDIO,
    ".aac": AttachmentKind.AUDIO,
    ".webm": AttachmentKind.AUDIO,
    ".flac": AttachmentKind.AUDIO,
}

AUDIO_EXTENSIONS = frozenset(
    ext for ext, kind in ALLOWED_EXTENSIONS.items() if kind == AttachmentKind.AUDIO
)
IMAGE_EXTENSIONS = frozenset(
    ext for ext, kind in ALLOWED_EXTENSIONS.items() if kind == AttachmentKind.IMAGE
)

#: Тип по расширению. Источник истины — проверенный белый список,
#: а не заголовок Content-Type от клиента: иначе файл с расширением
#: .png, объявленный как text/html, отдался бы браузеру как страница.
MIME_BY_EXTENSION: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".zip": "application/zip",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
}


def detect_mime_type(filename: str) -> str:
    """Тип файла строго по расширению."""
    ext = Path(filename).suffix.lower()
    known = MIME_BY_EXTENSION.get(ext)
    if known:
        return known
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


#: Ожидаемое содержимое: расширение не должно совпадать с исполняемым кодом.
BLOCKED_SIGNATURES: tuple[bytes, ...] = (
    b"MZ",
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",
    b"#!",
)


def sanitize_filename(name: str | None) -> str:
    """Убирает путь и опасные символы. На диск попадает только base name."""
    raw = (name or "file").strip()
    raw = raw.replace("\\", "/").split("/")[-1]
    safe = "".join(ch for ch in raw if ch.isprintable() and ch not in '<>:"|?*')
    safe = safe.strip(". ")
    if not safe:
        safe = "file"
    return safe[:200]


def detect_kind(filename: str, mime_type: str | None) -> AttachmentKind:
    ext = Path(filename).suffix.lower()
    kind = ALLOWED_EXTENSIONS.get(ext)
    if kind is not None:
        return kind
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type.split(";")[0].strip())
        if guessed:
            kind = ALLOWED_EXTENSIONS.get(guessed.lower())
            if kind is not None:
                return kind
    if mime_type and mime_type.startswith("image/"):
        return AttachmentKind.IMAGE
    if mime_type and mime_type.startswith("audio/"):
        return AttachmentKind.AUDIO
    return AttachmentKind.OTHER


def ensure_extension_allowed(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise bad_request(
            "extension_not_allowed",
            f"Расширение {ext} не поддерживается",
            {"allowed": sorted(ALLOWED_EXTENSIONS)},
        )
    if not ext:
        raise bad_request(
            "extension_required",
            "Файл должен иметь расширение",
            {"allowed": sorted(ALLOWED_EXTENSIONS)},
        )
    return ext


def check_signature(data: bytes, filename: str) -> None:
    """Отсекаем исполняемые файлы, даже если их переименовали в .jpg."""
    for signature in BLOCKED_SIGNATURES:
        if data.startswith(signature):
            raise bad_request(
                "content_not_allowed",
                "Содержимое файла похоже на исполняемый код и не принимается",
            )
    if not data:
        raise bad_request("file_empty", "Файл пуст")


def storage_dir() -> Path:
    path = get_settings().storage_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _shard(name: str) -> Path:
    """Раскладываем файлы по подпапкам: иначе каталог с 100k файлов."""
    return storage_dir() / name[:2] / name[2:4]


_CHUNK_SIZE = 1024 * 1024


async def read_upload(file: UploadFile) -> bytes:
    """Читает загрузку, прерываясь на первом же превышении лимита.

    Раньше файл целиком материализовался в памяти и только потом
    сравнивался с лимитом: отправка тела в гигабайты успевала израсходовать
    память процесса, хотя ответ должен был быть 413.
    """
    limit = get_settings().max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(min(_CHUNK_SIZE, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            await file.close()
            raise too_large(f"Файл больше {get_settings().max_upload_mb} МБ")
        chunks.append(chunk)
    return b"".join(chunks)


async def save_upload(
    data: bytes,
    filename: str,
    uploader: User,
    *,
    mime_type: str | None = None,
    task_id: str | None = None,
    meta: dict | None = None,
) -> Attachment:
    settings = get_settings()

    if len(data) > settings.max_upload_bytes:
        raise too_large(
            f"Файл больше {settings.max_upload_mb} МБ"
        )
    if not data:
        raise bad_request("file_empty", "Файл пуст")

    safe_name = sanitize_filename(filename)
    ensure_extension_allowed(safe_name)
    check_signature(data, safe_name)

    kind = detect_kind(safe_name, mime_type)
    stored_name = f"{uuid.uuid4().hex}{Path(safe_name).suffix.lower()}"
    target_dir = _shard(stored_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / stored_name

    # Пишем через временный файл: оборванная загрузка не оставит мусора.
    temp = target.with_suffix(target.suffix + ".part")
    with open(temp, "wb") as fh:
        fh.write(data)
    os.replace(temp, target)

    checksum = hashlib.sha256(data).hexdigest()
    width = height = duration = None
    extra: dict = dict(meta or {})

    if kind == AttachmentKind.IMAGE:
        width, height = _image_size(data)
        if width and height:
            extra.update({"width": width, "height": height})
        extra["preview_name"] = _make_preview(data, stored_name, target_dir)

    if kind == AttachmentKind.AUDIO:
        duration = _audio_duration(data)
        if duration:
            extra["duration_sec"] = duration

    attachment = Attachment(
        id=uuid.uuid4().hex,
        kind=kind,
        original_name=safe_name,
        stored_name=stored_name,
        # Тип берём из расширения: заголовок от клиента может врать.
        mime_type=detect_mime_type(safe_name),
        size_bytes=len(data),
        checksum=checksum,
        preview_name=extra.get("preview_name"),
        uploader_id=uploader.id,
        task_id=task_id,
        meta=extra,
    )
    return attachment


def _image_size(data: bytes) -> tuple[int | None, int | None]:
    """Размеры PNG/JPEG/GIF без внешних библиотек."""
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(io.BytesIO(data)) as img:
            return img.width, img.height
    except ImportError:
        pass
    except Exception:
        return None, None

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) >= 24:
            return (
                int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"),
            )
    if data[:3] == b"GIF":
        if len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        index = 2
        while index < len(data) - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                return (
                    int.from_bytes(data[index + 7 : index + 9], "big"),
                    int.from_bytes(data[index + 5 : index + 7], "big"),
                )
            length = int.from_bytes(data[index + 2 : index + 4], "big")
            index += 2 + length
    return None, None


def _make_preview(data: bytes, stored_name: str, target_dir: Path) -> str | None:
    """Превью для больших фото. Без Pillow превью не делаем — не критично."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return None

    settings = get_settings()
    preview_name = f"{stored_name}.thumb.jpg"
    preview_path = target_dir / preview_name
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.thumbnail((settings.preview_max_edge, settings.preview_max_edge))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(preview_path, "JPEG", quality=82)
    except Exception:
        return None
    return preview_name


def _audio_duration(data: bytes) -> float | None:
    """Длительность аудио через PyAV: отдельный ffmpeg не нужен."""
    try:
        import av  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        with av.open(io.BytesIO(data)) as container:
            if container.duration:
                return round(container.duration / 1_000_000, 2)
            stream = next(
                (s for s in container.streams if s.type in ("audio", "video")), None
            )
            if stream is not None and stream.duration and stream.time_base:
                return round(float(stream.duration * stream.time_base), 2)
    except Exception:
        return None
    return None


def file_path(attachment: Attachment) -> Path:
    path = _shard(attachment.stored_name) / attachment.stored_name
    if not path.exists():
        raise not_found("file_missing", "Файл не найден на диске")
    return path


def preview_path(attachment: Attachment) -> Path | None:
    if not attachment.preview_name:
        return None
    path = _shard(attachment.stored_name) / attachment.preview_name
    return path if path.exists() else None


def delete_files(attachment: Attachment) -> None:
    # Раньше здесь стояло `contextlib.suppress(not_found)`, но not_found —
    # функция, возвращающая исключение, а не класс. Передача её в
    # suppress приводила к TypeError вместо тихого пропуска, и удаление
    # уже отсутствующего на диске файла отдавало 500.
    with contextlib.suppress(AppError):
        file_path(attachment).unlink(missing_ok=True)
    preview = preview_path(attachment)
    if preview:
        preview.unlink(missing_ok=True)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def cleanup_orphans(max_age_days: int = 7) -> int:
    """Удаляет .part-файлы, оставшиеся после прерванной загрузки."""
    from datetime import timedelta

    from app.database import utcnow

    cutoff = utcnow() - timedelta(days=max_age_days)
    removed = 0
    root = storage_dir()
    for part in root.rglob("*.part"):
        try:
            if datetime.fromtimestamp(part.stat().st_mtime) < cutoff:
                part.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def storage_usage() -> dict[str, int]:
    root = storage_dir()
    total = 0
    files = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            files += 1
    return {"files": files, "bytes": total}


def copy_tree(source: Path, destination: Path) -> None:
    """Копирование дерева файлов в бэкап с сохранением структуры."""
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
