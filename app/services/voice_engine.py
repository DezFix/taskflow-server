"""Распознавание голосовых сообщений на сервере (faster-whisper).

Модель работает локально: интернет и API-ключи не нужны, записи никуда
не уходят. Модель скачивается один раз в MODELS_DIR и кэшируется между
перезапусками.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger("voice")

# Whisper принимает 16 кГц моно; модель сама ресемплирует, но мы приводим
# звук к нужному виду заранее — так результат стабильнее.
TARGET_SAMPLE_RATE = 16_000

#: Модели, доступные в faster-whisper. small — рабочий компромисс
#: между точностью и скоростью на CPU.
AVAILABLE_MODELS = ("tiny", "base", "small", "medium")
COMPUTE_TYPES = ("int8", "int8_float16", "float16", "float32")


@dataclass
class TranscriptResult:
    text: str
    language: str | None = None
    duration_sec: float | None = None
    confidence: float | None = None
    model_name: str | None = None
    compute_type: str | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)


class VoiceEngine:
    """Ленивая загрузка модели и последовательный запуск распознавания.

    Whisper нельзя безопасно вызывать из нескольких потоков одновременно,
    поэтому тяжёлая работа уходит в пул потоков, а экземпляр модели
    защищён блокировкой.
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._model_name: str | None = None
        self._compute_type: str | None = None
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutorSingleton.get()
        self._settings = get_settings()

    # --- Параметры ---

    def current_settings(self) -> dict[str, Any]:
        settings = get_settings()
        return {
            "enabled": settings.voice_enabled,
            "model": settings.voice_model,
            "language": settings.voice_language,
            "compute_type": settings.voice_compute_type,
        }

    #: Параметр в настройках -> имя переменной окружения.
    SETTINGS_TO_ENV = {
        "enabled": "VOICE_ENABLED",
        "model": "VOICE_MODEL",
        "language": "VOICE_LANGUAGE",
        "compute_type": "VOICE_COMPUTE_TYPE",
    }

    def update_settings(self, **changes: Any) -> dict[str, Any]:
        """Меняет параметры в файле .env, чтобы они пережили перезапуск."""
        settings = get_settings()
        env_file = settings.path("./.env")

        # Собираем только те переменные, которые реально меняем.
        wanted: dict[str, str] = {}
        for name, value in changes.items():
            if value is None or name not in self.SETTINGS_TO_ENV:
                continue
            wanted[self.SETTINGS_TO_ENV[name]] = _format_env(value)

        lines: list[str] = []
        replaced: set[str] = set()

        if env_file.exists():
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                stripped = raw.strip()
                if stripped and not stripped.startswith("#") and "=" in raw:
                    key = raw.split("=", 1)[0].strip()
                    if key in wanted:
                        raw = f"{key}={wanted[key]}"
                        replaced.add(key)
                lines.append(raw)

        for key, value in wanted.items():
            if key not in replaced:
                lines.append(f"{key}={value}")

        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Перечитываем настройки и сбрасываем кэш модели при смене весов.
        get_settings.cache_clear()
        self._settings = get_settings()
        if "model" in changes or "compute_type" in changes:
            self._model_name = None
            self._model = None
        return self.current_settings()

    def is_loaded(self) -> bool:
        return self._model is not None

    def model_info(self) -> dict[str, Any]:
        return {
            "loaded": self.is_loaded(),
            "model": self._model_name or get_settings().voice_model,
            "compute_type": self._compute_type or get_settings().voice_compute_type,
        }

    # --- Загрузка модели ---

    def _load_model(self) -> Any:
        settings = get_settings()
        model_name = settings.voice_model
        compute_type = settings.voice_compute_type

        if self._model is not None and self._model_name == model_name:
            return self._model

        with self._lock:
            if self._model is not None and self._model_name == model_name:
                return self._model

            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - зависит от установки
                raise RuntimeError(
                    "Модуль faster-whisper не установлен. "
                    "Установите: pip install faster-whisper"
                ) from exc

            models_dir = get_settings().models_path
            models_dir.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("HF_HOME", str(models_dir))

            logger.info("voice_model_loading", model=model_name, compute_type=compute_type)
            try:
                # cpu_threads=0 отдаёт выбор ядрам; int8 — приемлемое качество.
                model = WhisperModel(
                    model_name,
                    device="cpu",
                    compute_type=compute_type,
                    download_root=str(models_dir),
                    cpu_threads=max(1, (os.cpu_count() or 2) - 1),
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Не удалось загрузить модель Whisper '{model_name}': {exc}. "
                    "Проверьте доступ к сети при первом запуске."
                ) from exc

            self._model = model
            self._model_name = model_name
            self._compute_type = compute_type
            logger.info("voice_model_ready", model=model_name, compute_type=compute_type)
            return model

    # --- Аудио ---

    def _decode_to_array(self, data: bytes):
        """Декодирует аудио в numpy-массив: 16 кГц, моно, float32.

        Whisper принимает готовый массив и не требует файла. Так надёжнее,
        чем собирать WAV в памяти: не нужно муксить контейнер, а значит
        не возникнет битого заголовка. PyAV входит в зависимости сервера,
        поэтому внешний ffmpeg не требуется.
        """
        import io

        import numpy as np

        if not data:
            # Пустая запись: возвращаем пустой массив, чтобы вызывающий
            # получил понятный результат, а не ошибку декодера.
            return np.zeros(0, dtype=np.float32)

        try:
            import av  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("Модуль PyAV не установлен. Установите: pip install av") from exc

        chunks: list = []
        resampler = av.audio.resampler.AudioResampler(
            format="s16",
            layout="mono",
            rate=TARGET_SAMPLE_RATE,
        )

        with av.open(io.BytesIO(data)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise RuntimeError("В файле нет аудиодорожки")

            for frame in container.decode(stream):
                for resampled in resampler.resample(frame):
                    chunks.append(resampled)
            # Хвост ресемплера: без него теряется последняя доля секунды.
            for resampled in resampler.resample(None):
                chunks.append(resampled)

        if not chunks:
            return np.zeros(0, dtype=np.float32)

        samples = np.concatenate(
            [frame.to_ndarray().reshape(-1) for frame in chunks]
        ).astype(np.float32)
        # s16 -> float32 в диапазоне [-1, 1].
        return samples / 32768.0

    def _normalize_audio(self, data: bytes) -> tuple[bytes, float]:
        """Совместимость: возвращает WAV-байты и длительность.

        Нужен только для тестов и отладки: самому Whisper массив удобнее.
        """
        import io
        import wave

        samples = self._decode_to_array(data)
        duration = round(len(samples) / TARGET_SAMPLE_RATE, 2)

        pcm = (samples * 32767.0).astype("int16").tobytes()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(TARGET_SAMPLE_RATE)
            handle.writeframes(pcm)
        return buffer.getvalue(), duration

    # --- Распознавание ---

    def _transcribe_sync(
        self, data: bytes, model_name: str | None, language: str | None
    ) -> TranscriptResult:
        model = self._load_model()
        settings = get_settings()

        samples = self._decode_to_array(data)
        if samples.size == 0:
            return TranscriptResult(
                text="",
                language=language,
                duration_sec=0.0,
                model_name=model_name or settings.voice_model,
                compute_type=self._compute_type or settings.voice_compute_type,
            )

        duration = round(samples.size / TARGET_SAMPLE_RATE, 2)

        # Язык по умолчанию берём из настроек: пустой результат из-за
        # неверного языка — самая частая причина «пустых» расшифровок.
        source_language = (language or settings.voice_language or "").strip() or None

        segments, info = model.transcribe(
            samples,
            language=source_language,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )

        texts: list[str] = []
        confidences: list[float] = []
        details: list[dict[str, Any]] = []
        for segment in segments:
            piece = (segment.text or "").strip()
            if piece:
                texts.append(piece)
                avg_logprob = getattr(segment, "avg_logprob", None)
                if avg_logprob is not None:
                    # Из log-вероятности в понятную шкалу 0..1.
                    import math

                    confidences.append(max(0.0, min(1.0, math.exp(avg_logprob))))
                details.append({"start": segment.start, "end": segment.end, "text": piece})

        text = " ".join(texts).strip()
        avg_confidence = sum(confidences) / len(confidences) if confidences else None

        return TranscriptResult(
            text=text,
            language=getattr(info, "language", None),
            duration_sec=getattr(info, "duration", None) or duration,
            confidence=round(avg_confidence, 3) if avg_confidence is not None else None,
            model_name=model_name or settings.voice_model,
            compute_type=self._compute_type or settings.voice_compute_type,
            segments=details,
        )

    async def transcribe(
        self,
        data: bytes,
        *,
        model_name: str | None = None,
        language: str | None = None,
    ) -> TranscriptResult:
        """Асинхронная обёртка: тяжёлая работа уходит в отдельный поток."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self._transcribe_sync(data, model_name, language)
        )

    async def warmup(self) -> None:
        """Прогрев модели в фоне при старте сервера.

        Первый запрос иначе ждал бы скачивание весов на несколько минут.
        """
        if not get_settings().voice_enabled:
            return
        try:
            await asyncio.to_thread(self._load_model)
        except Exception as exc:
            logger.warning("voice_warmup_failed", error=str(exc))

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._model_name = None
            logger.info("voice_model_unloaded")


def _format_env(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class ThreadPoolExecutorSingleton:
    """Один пул на весь процесс: whisper занимает все ядра сам."""

    _executor: Any = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> Any:
        if cls._executor is None:
            with cls._lock:
                if cls._executor is None:
                    from concurrent.futures import ThreadPoolExecutor

                    # Whisper сам использует все ядра, поэтому потоков мало.
                    cls._executor = ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="voice"
                    )
        return cls._executor


engine = VoiceEngine()


async def get_status(message_id: str) -> dict[str, Any]:
    """Заглушка чтения статуса: реальную логику держит services.voice_jobs."""
    return {"message_id": message_id, "status": "pending"}


def supported_audio_extensions() -> list[str]:
    """Расширения без точки — так удобнее сравнивать в клиенте."""
    from app.services.files import AUDIO_EXTENSIONS

    return sorted(ext.lstrip(".") for ext in AUDIO_EXTENSIONS)


def is_voice_candidate(filename: str, mime_type: str | None) -> bool:
    from app.services.files import AUDIO_EXTENSIONS

    if Path(filename).suffix.lower() in AUDIO_EXTENSIONS:
        return True
    return bool(mime_type and mime_type.startswith("audio/"))


def model_disk_usage() -> dict[str, int]:
    total = 0
    files = 0
    root = get_settings().models_path
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
                files += 1
    return {"files": files, "bytes": total}


def warmup_note() -> str:
    settings = get_settings()
    if not settings.voice_enabled:
        return "Распознавание выключено (VOICE_ENABLED=false)"
    return (
        f"Модель '{settings.voice_model}' будет загружена при первом голосовом "
        "сообщении. Первый запуск может занять несколько минут."
    )


def _unused_datetime_guard() -> datetime:  # pragma: no cover
    return datetime.now()
