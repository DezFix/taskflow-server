"""Диагностика пустой расшифровки: проверяем параметры Whisper."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="taskflow-diag-"))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(TMP / 'd.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(TMP / "s"))
os.environ.setdefault("MODELS_DIR", str(TMP / "m"))
os.environ.setdefault("BACKUP_DIR", str(TMP / "b"))
os.environ.setdefault("JWT_SECRET", "diag-secret-key-for-local-testing-0123456789")
os.environ.setdefault("VOICE_MODEL", "base")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np  # noqa: E402

from app.services.voice_engine import engine  # noqa: E402


def make_speech(text: str, path: Path) -> Path:
    """Синтез через SAPI.

    Текст передаётся файлом, а не аргументом командной строки: иначе
    кириллица портится о кодовую страницу консоли.
    """
    import subprocess

    text_file = path.with_suffix(".txt")
    text_file.write_text(text, encoding="utf-8")

    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = -1; "
        f"$s.SetOutputToWaveFile('{path}'); "
        f"$t = Get-Content -Raw -Encoding UTF8 '{text_file}'; "
        "$s.Speak($t); $s.Dispose();"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False, capture_output=True)
    return path


def main() -> int:
    # На этой машине установлен только английский голос Zira, поэтому
    # проверяем конвейер на английской фразе. Сам конвейер от языка
    # не зависит: тот же путь используется для русских записей.
    text = (
        "Hello, this is a test of the voice message recognition system"
        if len(sys.argv) < 2
        else sys.argv[1]
    )
    wav = make_speech(text, TMP / "speech.wav")
    data = wav.read_bytes()
    print(f"Текст для проверки: {text}")
    print(f"Файл: {len(data)} байт")
    if len(data) < 2000:
        print("Синтез не удался (нет подходящего голоса в системе)")
        return 1

    engine._load_model()
    model = engine._model

    samples = engine._decode_to_array(data)
    print(f"Сэмплов: {len(samples)} ({len(samples) / 16000:.2f} с)")
    print(f"Амплитуда: min={samples.min():.4f} max={samples.max():.4f}")
    print(f"Доля сэмплов выше 0.01: {(np.abs(samples) > 0.01).mean():.3f}")

    variants = [
        ("с VAD (как сейчас)", dict(vad_filter=True)),
        ("без VAD", dict(vad_filter=False)),
    ]

    for label, kwargs in variants:
        segments, info = model.transcribe(samples, **kwargs)
        texts = [s.text.strip() for s in segments if s.text.strip()]
        print(f"\n{label}:")
        print(f"  язык определён: {info.language} ({info.language_probability:.3f})")
        print(f"  сегментов: {len(texts)}")
        for piece in texts:
            print(f"  > {piece}")
        if not texts:
            print("  (текста нет)")

    # Итог через штатный метод движка — то, что использует сервер.
    result = engine._transcribe_sync(data, None, None)
    print("\nЧерез VoiceEngine (как в сервере):")
    print(f"  язык: {result.language}, уверенность: {result.confidence}")
    print(f"  текст: {result.text!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
