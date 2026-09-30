"""Очередь распознавания голосовых сообщений.

Распознавание идёт фоном: сообщение сразу появляется в чате со статусом
«распознаётся», а текст приходит событием в WebSocket, когда готов.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.database import utcnow
from app.logging_setup import get_logger
from app.models import Attachment, Message, Transcript, TranscriptStatus, User
from app.realtime import EVENT_TRANSCRIPT_READY, hub
from app.security import new_id
from app.services import files as files_service
from app.services.voice_engine import engine

logger = get_logger("voice_jobs")

#: Пауза между завершённой и следующей задачей: CPU не перегревается.
COOLDOWN_SECONDS = 0.5

#: Сколько ждать появления записи расшифровки, пока не завершится
#: транзакция запроса, создавшего голосовое сообщение.
TRANSCRIPT_WAIT_ATTEMPTS = 10
TRANSCRIPT_WAIT_SECONDS = 0.3


class VoiceQueue:
    """Последовательная очередь: одна модель, один тяжёлый вызов за раз."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._running = False
        self._priority: list[str] = []

    def bind(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = asyncio.create_task(self._run(), name="voice-queue")

    async def stop(self) -> None:
        self._running = False
        await self._queue.put(None)
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._worker, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._worker.cancel()
            self._worker = None

    async def enqueue(self, message_id: str, *, priority: bool = False) -> None:
        """Ставит сообщение в очередь.

        Приоритетные задания (повтор после ошибки) обрабатываются раньше
        обычной очереди, чтобы сотрудник не ждал конца длинной очереди.
        Важно: id кладётся только в один из списков, иначе задание
        выполнится дважды.
        """
        if message_id in self._priority:
            return
        if priority:
            self._priority.append(message_id)
            return
        await self._queue.put(message_id)

    def _next(self) -> str | None:
        return self._priority.pop(0) if self._priority else None

    async def _run(self) -> None:
        while self._running:
            # Счётчик незавершённых задач увеличивает только обычная
            # очередь. Приоритетная — обычный список, поэтому для неё
            # task_done() звать нельзя: он уменьшил бы счётчик чужой
            # задачи и сломал бы join() у ожидающих.
            from_queue = False
            message_id = self._next()
            if message_id is None:
                try:
                    message_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    from_queue = True
                except TimeoutError:
                    continue
            if message_id is None:
                break
            try:
                await self._process(message_id)
            except Exception:
                logger.exception("voice_job_failed", message_id=message_id)
            finally:
                if from_queue:
                    self._queue.task_done()
                await asyncio.sleep(COOLDOWN_SECONDS)

    async def _process(self, message_id: str) -> None:
        if self._sessionmaker is None:
            logger.error("voice_queue_not_bound")
            return

        async with self._sessionmaker() as session:
            # Голосовое ставится в очередь внутри транзакции запроса, а
            # воркер может проснуться раньше, чем она закоммитится. Поэтому
            # отсутствие записи — это не ошибка, а повод подождать.
            transcript = await self._await_transcript(session, message_id)
            if transcript is None:
                logger.warning("voice_transcript_missing", message_id=message_id)
                return

            message = await session.get(Message, message_id)
            if message is None or message.attachment_id is None:
                logger.warning("voice_message_missing", message_id=message_id)
                return

            attachment = await session.get(Attachment, message.attachment_id)
            if attachment is None:
                logger.warning("voice_attachment_missing", message_id=message_id)
                return

            if not get_settings().voice_enabled:
                transcript.status = TranscriptStatus.SKIPPED
                transcript.error = "Распознавание отключено на сервере"
                transcript.finished_at = utcnow()
                await session.commit()
                await self._notify(message, transcript)
                return

            transcript.status = TranscriptStatus.RUNNING
            transcript.attempts += 1
            transcript.started_at = utcnow()
            transcript.error = None
            await session.commit()

            try:
                data = files_service.file_path(attachment).read_bytes()
            except Exception as exc:
                await self._fail(session, transcript, message, f"Файл недоступен: {exc}")
                return

            try:
                result = await engine.transcribe(
                    data,
                    language=transcript.language or get_settings().voice_language,
                )
            except Exception as exc:
                logger.warning("voice_transcribe_error", error=str(exc))
                await self._fail(session, transcript, message, str(exc))
                return

            transcript.status = TranscriptStatus.DONE
            transcript.text = result.text
            transcript.language = result.language
            transcript.model_name = result.model_name
            transcript.compute_type = result.compute_type
            transcript.duration_sec = result.duration_sec
            transcript.confidence = result.confidence
            transcript.finished_at = utcnow()
            transcript.error = None

            if result.text:
                message.transcript_text = result.text
                message.voice_duration_sec = result.duration_sec

            await session.commit()
            await self._notify(message, transcript)

    async def _await_transcript(
        self, session: AsyncSession, message_id: str
    ) -> Transcript | None:
        """Ждёт появления записи расшифровки.

        Транзакция запроса, создавшего голосовое, может ещё не завершиться,
        когда воркер возьмёт задание. Несколько попыток с паузой снимают
        гонку без изменения порядка операций на стороне API.
        """
        for attempt in range(TRANSCRIPT_WAIT_ATTEMPTS):
            transcript = await session.scalar(
                select(Transcript).where(Transcript.message_id == message_id)
            )
            if transcript is not None:
                return transcript
            if attempt < TRANSCRIPT_WAIT_ATTEMPTS - 1:
                await asyncio.sleep(TRANSCRIPT_WAIT_SECONDS)
        return None

    async def _fail(
        self, session: AsyncSession, transcript: Transcript, message: Message, error: str
    ) -> None:
        transcript.status = TranscriptStatus.FAILED
        transcript.error = error[:2000]
        transcript.finished_at = utcnow()
        await session.commit()
        await self._notify(message, transcript)

    async def _notify(self, message: Message, transcript: Transcript) -> None:
        from app.schemas.chat import TranscriptOut

        payload = {
            "message_id": message.id,
            "chat_id": message.chat_id,
            "transcript": TranscriptOut.model_validate(transcript).model_dump(),
        }
        members = await self._chat_member_ids(message.chat_id)
        await hub.send_to_users(members, EVENT_TRANSCRIPT_READY, payload)

    @staticmethod
    async def _chat_member_ids(chat_id: str) -> set[str]:
        from app.database import get_sessionmaker
        from app.models import ChatMember

        async with get_sessionmaker()() as session:
            rows = await session.scalars(
                select(ChatMember.user_id).where(
                    ChatMember.chat_id == chat_id, ChatMember.left_at.is_(None)
                )
            )
            return set(rows)


queue = VoiceQueue()


async def submit_transcription(
    session: AsyncSession,
    *,
    message: Message,
    attachment: Attachment,
    duration_sec: float | None = None,
    language: str | None = None,
) -> Transcript:
    """Создаёт запись расшифровки и ставит в очередь.

    Если распознавание выключено, помечаем пропущенным сразу: так клиент
    сразу видит причину, а не ждёт обработчика в очереди.
    """
    disabled = not get_settings().voice_enabled
    transcript = Transcript(
        id=new_id(),
        message_id=message.id,
        status=(
            TranscriptStatus.SKIPPED if disabled else TranscriptStatus.PENDING
        ),
        language=language or get_settings().voice_language,
        duration_sec=duration_sec,
        error="Распознавание отключено на сервере" if disabled else None,
        finished_at=utcnow() if disabled else None,
    )
    session.add(transcript)
    await session.flush()

    if not disabled:
        await queue.enqueue(message.id)
    return transcript


async def retry_transcription(session: AsyncSession, transcript: Transcript) -> None:
    """Повторный запуск после ошибки."""
    if transcript.status == TranscriptStatus.RUNNING:
        return
    transcript.status = TranscriptStatus.PENDING
    transcript.error = None
    transcript.started_at = None
    transcript.finished_at = None
    await session.flush()
    await queue.enqueue(transcript.message_id, priority=True)


async def save_manual_transcript(
    session: AsyncSession, transcript: Transcript, message: Message, text: str
) -> Transcript:
    """Ручная правка расшифровки: когда модель ошиблась."""
    transcript.text = text.strip()
    transcript.status = TranscriptStatus.DONE
    transcript.is_manual = True
    transcript.error = None
    transcript.finished_at = utcnow()
    message.transcript_text = transcript.text
    await session.flush()
    return transcript


def pending_count() -> int:
    return queue._queue.qsize() + len(queue._priority)


async def recover_orphans(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """После перезапуска возвращает в очередь незавершённые задания.

    Статус RUNNING означает, что процесс был убит посреди работы.
    """
    count = 0
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(Transcript).where(
                    Transcript.status.in_(
                        [TranscriptStatus.PENDING, TranscriptStatus.RUNNING]
                    )
                )
            )
        ).all()
        for transcript in rows:
            transcript.status = TranscriptStatus.PENDING
            transcript.error = None
            await queue.enqueue(transcript.message_id)
            count += 1
        if count:
            await session.commit()
    if count:
        logger.info("voice_jobs_recovered", count=count)
    return count


def _unused() -> datetime:  # pragma: no cover
    return utcnow()


def user_can_transcribe(user: User) -> bool:
    return "voice.transcribe" in user.permissions
