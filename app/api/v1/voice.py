"""Голос: настройки распознавания, статус и повтор расшифровки."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.config import get_settings
from app.deps import SessionDep, require_perm
from app.errors import bad_request, forbidden, not_found
from app.models import Message, Transcript, TranscriptStatus, User
from app.schemas import (
    OkMessage,
    VoiceJobOut,
    VoiceSettingsOut,
    VoiceSettingsUpdate,
)
from app.services import chat as chat_service
from app.services import voice_engine, voice_jobs
from app.services.voice_engine import AVAILABLE_LANGUAGES, AVAILABLE_MODELS, COMPUTE_TYPES

router = APIRouter(prefix="/voice", tags=["voice"])

#: Больше пяти попыток бессмысленно: запись повреждена или модель короче не справится.
MAX_ATTEMPTS = 5


@router.get(
    "/settings",
    response_model=VoiceSettingsOut,
    summary="Параметры распознавания",
    description=(
        "Модель по умолчанию — base: быстро и достаточно точно. "
        "small и medium точнее, но требуют больше времени на CPU."
    ),
)
async def get_voice_settings(
    _: User = Depends(require_perm("settings.view")),
) -> VoiceSettingsOut:
    settings = get_settings()
    return VoiceSettingsOut(
        enabled=settings.voice_enabled,
        model=settings.voice_model,
        language=settings.voice_language,
        compute_type=settings.voice_compute_type,
        available_models=list(AVAILABLE_MODELS),
        models_dir=str(settings.models_path),
    )


@router.put(
    "/settings",
    response_model=VoiceSettingsOut,
    summary="Изменить параметры распознавания",
    description="Параметры сохраняются в .env и переживают перезапуск сервера.",
)
async def update_voice_settings(
    payload: VoiceSettingsUpdate,
    _: User = Depends(require_perm("settings.edit")),
) -> VoiceSettingsOut:
    if payload.model and payload.model not in AVAILABLE_MODELS:
        raise bad_request(
            "model_invalid",
            f"Модель должна быть одной из: {', '.join(AVAILABLE_MODELS)}",
        )
    if payload.compute_type and payload.compute_type not in COMPUTE_TYPES:
        raise bad_request(
            "compute_type_invalid",
            f"Режим вычислений должен быть одним из: {', '.join(COMPUTE_TYPES)}",
        )
    if payload.language and payload.language not in AVAILABLE_LANGUAGES:
        # Раньше язык оставался единственным из четырёх полей без проверки:
        # модель и режим вычислений сверялись со списком, а язык уходил в
        # файл .env как есть.
        raise bad_request(
            "language_invalid",
            f"Язык должен быть одним из: {', '.join(AVAILABLE_LANGUAGES)}",
        )

    voice_engine.engine.update_settings(
        enabled=payload.enabled,
        model=payload.model,
        language=payload.language,
        compute_type=payload.compute_type,
    )

    settings = get_settings()
    return VoiceSettingsOut(
        enabled=settings.voice_enabled,
        model=settings.voice_model,
        language=settings.voice_language,
        compute_type=settings.voice_compute_type,
        available_models=list(AVAILABLE_MODELS),
        models_dir=str(settings.models_path),
    )


@router.get("/status", summary="Состояние движка распознавания")
async def engine_status(
    _: User = Depends(require_perm("settings.view")),
) -> dict:
    return {
        **voice_engine.engine.model_info(),
        **voice_engine.engine.current_settings(),
        "pending": voice_jobs.pending_count(),
        "disk": voice_engine.model_disk_usage(),
        "formats": voice_engine.supported_audio_extensions(),
        "note": voice_engine.warmup_note(),
    }


@router.post(
    "/warmup",
    response_model=OkMessage,
    summary="Прогреть модель заранее",
    description=(
        "Скачивает и загружает модель, не дожидаясь первого голосового. "
        "Первый запуск занимает несколько минут."
    ),
)
async def warmup_model(
    _: User = Depends(require_perm("settings.edit")),
) -> OkMessage:
    await voice_engine.engine.warmup()
    return OkMessage(detail="Модель загружена")


@router.get(
    "/{message_id}",
    response_model=VoiceJobOut,
    summary="Статус расшифровки сообщения",
    description=(
        "Статусы: pending — в очереди, running — распознаётся, "
        "done — готов, failed — ошибка, skipped — распознавание выключено."
    ),
)
async def transcript_status(
    message_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> VoiceJobOut:
    message = await session.get(Message, message_id)
    if message is None:
        raise not_found("message_not_found", "Сообщение не найден")

    if user.id not in await chat_service.member_ids(session, message.chat_id):
        raise forbidden("chat_access_denied", "Вы не участник этого чата")

    transcript = await session.scalar(
        select(Transcript).where(Transcript.message_id == message_id)
    )
    if transcript is None:
        raise not_found("transcript_missing", "Сообщение не является голосовым")

    return VoiceJobOut(
        message_id=message_id,
        chat_id=message.chat_id,
        status=transcript.status.value,
        text=transcript.text,
        error=transcript.error,
        model_name=transcript.model_name,
    )


@router.post(
    "/{message_id}/retry",
    response_model=OkMessage,
    summary="Повторить распознавание",
    description="Ставит сообщение в начало очереди.",
)
async def retry_transcript(
    message_id: str,
    session: SessionDep,
    _: User = Depends(require_perm("voice.transcribe")),
) -> OkMessage:
    transcript = await session.scalar(
        select(Transcript).where(Transcript.message_id == message_id)
    )
    if transcript is None:
        raise not_found("transcript_missing", "Расшифровка не найдена")
    if transcript.status == TranscriptStatus.DONE:
        raise bad_request("already_done", "Сообщение уже расшифровано")
    if transcript.attempts >= MAX_ATTEMPTS:
        raise bad_request(
            "attempts_exhausted",
            f"Превышено число попыток ({MAX_ATTEMPTS}). Проверьте запись вручную.",
        )

    await voice_jobs.retry_transcription(session, transcript)
    return OkMessage(detail="Распознавание поставлено в очередь")


@router.get("/queue/pending", summary="Состояние очереди распознавания")
async def queue_status(
    _: User = Depends(require_perm("settings.view")),
) -> dict:
    return {
        "pending": voice_jobs.pending_count(),
        "engine": voice_engine.engine.model_info(),
    }
