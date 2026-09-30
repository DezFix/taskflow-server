"""Проверка распознавания голоса на настоящем аудио.

Скрипт синтезирует речь через Windows SAPI, отправляет её в движок
faster-whisper и печатает распознанный текст. Нужен для проверки, что
модель скачалась, PyAV корректно нормализует звук, а очередь работает.

    python scripts/voice_check.py            # модель base
    python scripts/voice_check.py small      # более точная модель
    python scripts/voice_check.py --queue    # проверить очередь целиком
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="taskflow-voice-"))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(TMP / 'voice.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(TMP / "storage"))
os.environ.setdefault("MODELS_DIR", str(TMP / "models"))
os.environ.setdefault("BACKUP_DIR", str(TMP / "backups"))
os.environ.setdefault("JWT_SECRET", "voice-check-secret-key-for-local-testing-only")

# На этой машине установлен только английский голос SAPI, поэтому фразы
# для проверки английские. Сам конвейер от языка не зависит: для русских
# записей используется тот же код, только VOICE_LANGUAGE=ru.
PHRASES = [
    "Hello, this is a test of the voice message recognition",
    "The server is running and all services are available",
]
DEFAULT_LANGUAGE = "en"


def synthesize(text: str, path: Path) -> Path:
    """Синтезирует речь средствами Windows и сохраняет в WAV.

    ffmpeg не требуется: PyAV на сервере сам приводит звук к 16 кГц моно.
    Если SAPI недоступен, создаётся контрольный сигнал — проверяется
    путь декодирования, а не качество распознавания.
    """
    if sys.platform == "win32":
        import subprocess

        # Текст передаём файлом: кириллица в аргументах командной строки
        # портится о кодовую страницу консоли.
        text_file = path.with_suffix(".txt")
        text_file.write_text(text, encoding="utf-8")

        ps_script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.Rate = -1; "
            "$s.SetOutputToWaveFile('"
            + str(path)
            + "'); "
            "$t = Get-Content -Raw -Encoding UTF8 '"
            + str(text_file)
            + "'; "
            "$s.Speak($t); $s.Dispose();"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            check=False,
            capture_output=True,
        )
        if path.exists() and path.stat().st_size > 2000:
            return path

    _write_tone(path, seconds=3.0)
    return path



def _write_tone(path: Path, seconds: float = 3.0, freq: float = 440.0, rate: int = 16000) -> Path:
    """WAV с тоном: даёт модели ненулевой сигнал для проверки декодирования."""
    frames = int(rate * seconds)
    samples = bytearray()
    for index in range(frames):
        value = int(12000 * math.sin(2 * math.pi * freq * index / rate))
        samples += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(samples))
    return path


async def run(args: argparse.Namespace) -> int:
    import logging

    from app.config import get_settings
    from app.services.voice_engine import engine

    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.model:
        engine.update_settings(model=args.model)
    if args.language:
        engine.update_settings(language=args.language)

    settings = get_settings()
    print(f"Модель: {settings.voice_model}, вычисления: {settings.voice_compute_type}")
    print(f"Язык: {settings.voice_language}")
    print("Загрузка модели (первый раз скачивается с huggingface.co)...")

    try:
        engine._load_model()
    except Exception as exc:
        print(f"НЕ УДАЛОСЬ загрузить модель: {exc}")
        return 1

    print("Модель загружена.")
    print(f"Отслеживается: {engine.model_info()}")

    failures = 0
    for index, phrase in enumerate(PHRASES):
        wav_path = synthesize(phrase, TMP / f"phrase-{index}.wav")
        speech_ready = wav_path.exists() and wav_path.stat().st_size > 2000

        data = wav_path.read_bytes()
        print(f"\nАудио {index + 1}: {wav_path.name}, {len(data)} байт")
        print(f"  Ожидалось: {phrase!r}")

        if not speech_ready:
            print("  (синтез речи недоступен, используется контрольный сигнал)")

        result = await engine.transcribe(data)
        print(f"  Текст         : {result.text!r}")
        print(f"  Язык          : {result.language}")
        print(f"  Длительность  : {result.duration_sec} с")
        print(f"  Уверенность   : {result.confidence}")

        if not result.duration_sec:
            print("  -> ОШИБКА: аудио не декодировалось (длительность 0)")
            failures += 1
            continue

        print("  -> Декодирование и ресемплинг работают")

        if speech_ready:
            # Сверим ключевое слово из фразы с распознанным текстом.
            keyword = phrase.split()[0].lower().strip(".,")
            recognized = result.text.lower()
            if keyword and keyword in recognized:
                print(f"  -> Распознавание верное (найдено слово «{keyword}»)")
            else:
                print(f"  -> ВНИМАНИЕ: слово «{keyword}» не найдено в тексте")
                failures += 1

    if args.queue:
        print("\nПроверка очереди целиком")
        failures += await check_queue()

    print()
    print("Готово: все проверки пройдены." if not failures else f"Замечаний: {failures}")
    return 0 if not failures else 1


async def check_queue() -> int:
    """Отправляет голосовое через API и ждёт расшифровки в очереди."""
    import logging
    import socket

    import httpx
    import uvicorn

    from app.database import Base, get_engine, get_sessionmaker
    from app.main import app
    from app.models import Role, User
    from app.security import hash_password, new_id
    from app.services.voice_engine import engine

    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Схему создаём сами: пользователей заводим до старта сервера.
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with get_sessionmaker()() as session:
        role = Role(
            id=new_id(),
            key="Проверка",
            title="Проверка",
            permissions=["chat.direct", "files.upload", "tasks.view", "users.view"],
            is_system=False,
        )
        session.add(role)
        other = User(
            id=new_id(),
            username="voice_partner",
            full_name="Собеседник",
            password_hash=hash_password("Partner123"),
            is_active=True,
            must_change_password=False,
        )
        other.roles.append(role)
        sender = User(
            id=new_id(),
            username="voice_sender",
            full_name="Отправитель",
            password_hash=hash_password("Sender123"),
            is_active=True,
            must_change_password=False,
        )
        sender.roles.append(role)
        session.add_all([other, sender])
        await session.commit()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"identifier": "voice_sender", "password": "Sender123"},
            )
            if login.status_code != 200:
                print(f"  Вход не удался: {login.text}")
                return 1
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            users_list = await client.get("/api/v1/users?search=voice_", headers=headers)
            partner_id = next(
                item["id"]
                for item in users_list.json()
                if item["username"] == "voice_partner"
            )

            chat = await client.post(
                "/api/v1/chats/direct", json={"user_id": partner_id}, headers=headers
            )
            chat_id = chat.json()["id"]

            audio = _write_tone(TMP / "queue.wav", seconds=3.0).read_bytes()
            sent = await client.post(
                f"/api/v1/chats/{chat_id}/voice",
                files={"file": ("voice.wav", audio, "audio/wav")},
                data={"duration_sec": "3"},
                headers=headers,
            )
            if sent.status_code != 201:
                print(f"  Голосовое не принято: {sent.text}")
                return 1

            message_id = sent.json()["id"]
            print(f"  Голосовое принято, сообщение {message_id}")
            print(f"  Начальный статус: {sent.json()['transcript']['status']}")
            print(f"  Модель в движке: {engine.model_info()}")

            final = None
            previous = None
            for _ in range(180):  # до трёх минут на распознавание
                await asyncio.sleep(1)
                status = await client.get(f"/api/v1/voice/{message_id}", headers=headers)
                current = status.json()["status"]
                if current != previous:
                    print(f"  [{_:>3} c] статус: {current}")
                    previous = current
                if current in ("done", "failed", "skipped"):
                    final = status.json()
                    break


            if final is None:
                print("  Распознавание не завершилось за отведённое время")
                return 1

            print(f"  Итоговый статус: {final['status']}")
            if final["text"]:
                print(f"  Текст: {final['text']!r}")
            if final["error"]:
                print(f"  Ошибка: {final['error']}")
            return 0 if final["status"] == "done" else 1
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Проверка распознавания голоса")
    parser.add_argument("--model", default=None, help="tiny | base | small | medium")
    parser.add_argument(
        "--language",
        default=None,
        help="язык распознавания: ru (по умолчанию в сервере) или en для теста",
    )
    parser.add_argument("--queue", action="store_true", help="проверить очередь и API")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
