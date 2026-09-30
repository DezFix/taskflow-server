"""Тесты сервиса распознавания: настройки, нормализация аудио, отказоустойчивость."""

from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path

import pytest

from app.services import voice_engine as ve


def make_tone_wav(seconds: float = 2.0, freq: float = 440.0, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = int(rate * seconds)
        samples = bytearray()
        for index in range(frames):
            value = int(12000 * math.sin(2 * math.pi * freq * index / rate))
            samples += struct.pack("<h", value)
        handle.writeframes(bytes(samples))
    return buffer.getvalue()


def test_available_models_and_compute_types() -> None:
    assert {"tiny", "base", "small", "medium"} <= set(ve.AVAILABLE_MODELS)
    assert "int8" in ve.COMPUTE_TYPES


def test_current_settings_reflects_config() -> None:
    engine = ve.VoiceEngine()
    current = engine.current_settings()
    assert set(current) == {"enabled", "model", "language", "compute_type"}


def test_model_info_before_load() -> None:
    engine = ve.VoiceEngine()
    info = engine.model_info()
    assert info["loaded"] is False
    assert info["model"]  # имя модели известно до загрузки


def test_update_settings_does_not_duplicate_env_lines(tmp_path: Path) -> None:
    """Главный баг, который закрывает эта проверка: повторные вызовы не должны
    плодить дубли переменных в .env."""
    from app.config import get_settings

    env_file = get_settings().path("./.env")
    env_file.write_text("VOICE_MODEL=base\nVOICE_LANGUAGE=ru\n", encoding="utf-8")

    engine = ve.VoiceEngine()
    try:
        for _ in range(5):
            engine.update_settings(model="small")
        content = env_file.read_text(encoding="utf-8")

        assert content.count("VOICE_MODEL=") == 1, content
        assert "VOICE_MODEL=small" in content
        # Соседние настройки не должны теряться или дублироваться.
        assert content.count("VOICE_LANGUAGE=") == 1, content
        assert "VOICE_LANGUAGE=ru" in content
    finally:
        env_file.unlink(missing_ok=True)
        get_settings.cache_clear()


def test_update_settings_appends_missing_keys(tmp_path: Path) -> None:
    from app.config import get_settings

    env_file = get_settings().path("./.env")
    env_file.unlink(missing_ok=True)

    engine = ve.VoiceEngine()
    try:
        engine.update_settings(model="tiny", language="en")
        content = env_file.read_text(encoding="utf-8")
        assert "VOICE_MODEL=tiny" in content
        assert "VOICE_LANGUAGE=en" in content

        # Второй вызов не должен дублировать уже записанные ключи.
        engine.update_settings(compute_type="float32")
        content = env_file.read_text(encoding="utf-8")
        assert content.count("VOICE_MODEL=") == 1
        assert content.count("VOICE_LANGUAGE=") == 1
        assert "VOICE_COMPUTE_TYPE=float32" in content
    finally:
        env_file.unlink(missing_ok=True)
        get_settings.cache_clear()


def test_update_settings_formats_booleans() -> None:
    from app.config import get_settings

    env_file = get_settings().path("./.env")
    env_file.write_text("VOICE_ENABLED=true\n", encoding="utf-8")

    engine = ve.VoiceEngine()
    try:
        engine.update_settings(enabled=False)
        content = env_file.read_text(encoding="utf-8")
        assert "VOICE_ENABLED=false" in content
        assert content.count("VOICE_ENABLED=") == 1
    finally:
        env_file.unlink(missing_ok=True)
        get_settings.cache_clear()


def test_decode_audio_returns_mono_16k() -> None:
    """PyAV должен привести звук к 16 кГц моно — этого ждёт Whisper."""
    numpy = pytest.importorskip("numpy")
    engine = ve.VoiceEngine()

    samples = engine._decode_to_array(make_tone_wav(seconds=2.0))

    assert isinstance(samples, numpy.ndarray)
    assert samples.dtype == numpy.float32
    assert samples.size > 0
    # Две секунды с допуском на погрешность ресемплинга.
    assert abs(samples.size / ve.TARGET_SAMPLE_RATE - 2.0) < 0.2


def test_decode_audio_handles_stereo() -> None:
    """Запись с телефона может быть стерео — она тоже должна распознаваться."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        frames = 44100
        samples = bytearray()
        for index in range(frames):
            value = int(10000 * math.sin(2 * math.pi * 440 * index / 44100))
            samples += struct.pack("<hh", value, value)
        handle.writeframes(bytes(samples))

    engine = ve.VoiceEngine()
    samples_array = engine._decode_to_array(buffer.getvalue())
    assert samples_array.size > 0
    # 44100 -> 16000: примерно в 2.75 раза меньше кадров.
    assert samples_array.size < 44100


def test_normalize_audio_returns_valid_wav() -> None:
    engine = ve.VoiceEngine()
    wav_bytes, duration = engine._normalize_audio(make_tone_wav(seconds=1.0))

    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == ve.TARGET_SAMPLE_RATE
        assert handle.getnframes() > 0
    assert 0.5 < duration < 1.5


def test_decode_audio_without_audio_track_fails_clearly() -> None:
    """Пустой или битый файл должен давать понятную ошибку, а не стектрейс."""
    engine = ve.VoiceEngine()
    with pytest.raises(Exception) as info:
        engine._decode_to_array(b"not audio at all")
    message = str(info.value).lower()
    assert "аудио" in message or "invalid" in message or "нет" in message


def test_transcribe_empty_array_returns_empty_result() -> None:
    """Пустой файл не должен ронять движок: вернётся пустой результат."""
    engine = ve.VoiceEngine()
    # Подменяем загрузку модели, чтобы тест не качал веса.
    engine._load_model = lambda: object()  # type: ignore[method-assign]
    result = engine._transcribe_sync(b"", "base", "ru")
    assert result.text == ""
    assert result.duration_sec == 0.0


def test_supported_audio_extensions_have_no_dots() -> None:
    formats = ve.supported_audio_extensions()
    assert "m4a" in formats
    assert "ogg" in formats
    assert all(not item.startswith(".") for item in formats)


def test_is_voice_candidate() -> None:
    assert ve.is_voice_candidate("запись.m4a", None)
    assert ve.is_voice_candidate("record.wav", "audio/wav")
    assert not ve.is_voice_candidate("photo.png", "image/png")


def test_warmup_note_mentions_disabled_state() -> None:
    note = ve.warmup_note()
    assert "выключено" in note or "Модель" in note
