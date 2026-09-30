"""Тесты голосовых сообщений: очередь, статусы, настройки, ручная правка."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = pytest.mark.asyncio

# Минимальный корректный WAV-заголовок + тишина.
SILENT_WAV = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x40\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


def auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {users.tokens[role]}"}


async def _send_voice(client: AsyncClient, users, sender: str = "staff") -> dict:
    counterpart = "staff" if sender == "staff2" else "staff2"
    chat = await client.post(
        "/api/v1/chats/direct",
        json={"user_id": users.ids[counterpart]},
        headers=auth(users, sender),
    )
    assert chat.status_code == 200, chat.text
    response = await client.post(
        f"/api/v1/chats/{chat.json()['id']}/voice",
        files={"file": ("voice.wav", SILENT_WAV, "audio/wav")},
        data={"duration_sec": "1.0"},
        headers=auth(users, sender),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_voice_message_created(client: AsyncClient, users) -> None:
    message = await _send_voice(client, users)
    assert message["kind"] == "voice"
    assert message["transcript"] is not None
    assert message["transcript"]["status"] == "skipped"
    assert "отключено" in (message["transcript"]["error"] or "")


async def test_transcript_status_endpoint(client: AsyncClient, users) -> None:
    message = await _send_voice(client, users)
    response = await client.get(
        f"/api/v1/voice/{message['id']}", headers=auth(users, "staff")
    )
    assert response.status_code == 200
    assert response.json()["status"] == "skipped"


async def test_transcript_of_another_chat_is_denied(client: AsyncClient, users) -> None:
    message = await _send_voice(client, users)
    # Администратор не участник этого диалога.
    response = await client.get(f"/api/v1/voice/{message['id']}", headers=auth(users, "admin"))
    assert response.status_code == 403


async def test_status_for_text_message_is_404(client: AsyncClient, users) -> None:
    chat = await client.post(
        "/api/v1/chats/direct",
        json={"user_id": users.ids["staff2"]},
        headers=auth(users, "staff"),
    )
    text = await client.post(
        f"/api/v1/chats/{chat.json()['id']}/messages",
        json={"body": "не голосовое"},
        headers=auth(users, "staff"),
    )
    response = await client.get(
        f"/api/v1/voice/{text.json()['id']}", headers=auth(users, "staff")
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "transcript_missing"


async def test_retry_requires_permission(client: AsyncClient, users) -> None:
    """У роли «Сотрудник» нет voice.transcribe, поэтому повтор недоступен."""
    message = await _send_voice(client, users)
    response = await client.post(
        f"/api/v1/voice/{message['id']}/retry", headers=auth(users, "staff")
    )
    # У роли «Сотрудник» нет voice.transcribe.
    assert response.status_code == 403


async def test_retry_queues_message_once(client: AsyncClient, users) -> None:
    """Повтор ставит задание в очередь ровно один раз.

    Раньше приоритетная очередь клала идентификатор и в список, и в общую
    очередь, из-за чего сообщение распознавалось дважды.
    """
    from app.services.voice_jobs import queue

    message = await _send_voice(client, users)
    # Меняем статус на «ошибка», иначе сервер откажет с already_done.
    from app.database import get_sessionmaker
    from app.models import Transcript, TranscriptStatus

    async with get_sessionmaker()() as session:
        transcript = await session.scalar(
            select(Transcript).where(Transcript.message_id == message["id"])
        )
        transcript.status = TranscriptStatus.FAILED
        transcript.error = "Проверка повтора"
        await session.commit()

    before_priority = len(queue._priority)
    response = await client.post(
        f"/api/v1/voice/{message['id']}/retry", headers=auth(users, "head")
    )
    assert response.status_code == 200

    # Задание добавлено один раз и не продублировано в общую очередь.
    assert len(queue._priority) == before_priority + 1
    assert queue._priority.count(message["id"]) == 1

    # Повторный вызов не дублирует то же самое задание.
    from app.models import Transcript as _T

    async with get_sessionmaker()() as session:
        transcript = await session.scalar(
            select(_T).where(_T.message_id == message["id"])
        )
        transcript.status = TranscriptStatus.FAILED
        await session.commit()

    await client.post(
        f"/api/v1/voice/{message['id']}/retry", headers=auth(users, "head")
    )
    assert queue._priority.count(message["id"]) == 1

    # Убираем задание, чтобы фоновый обработчик его не подхватил.
    while message["id"] in queue._priority:
        queue._priority.remove(message["id"])


async def test_retry_rejects_already_done(client: AsyncClient, users) -> None:
    """Успешно распознанное сообщение не ставят на повтор."""
    from app.database import get_sessionmaker
    from app.models import Transcript, TranscriptStatus

    message = await _send_voice(client, users)
    async with get_sessionmaker()() as session:
        transcript = await session.scalar(
            select(Transcript).where(Transcript.message_id == message["id"])
        )
        transcript.status = TranscriptStatus.DONE
        transcript.text = "Готово"
        await session.commit()

    response = await client.post(
        f"/api/v1/voice/{message['id']}/retry", headers=auth(users, "head")
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "already_done"


async def test_retry_stops_after_max_attempts(client: AsyncClient, users) -> None:
    """После пяти попыток сервер перестаёт принимать повтор."""
    from app.database import get_sessionmaker
    from app.models import Transcript, TranscriptStatus

    message = await _send_voice(client, users)
    async with get_sessionmaker()() as session:
        transcript = await session.scalar(
            select(Transcript).where(Transcript.message_id == message["id"])
        )
        transcript.status = TranscriptStatus.FAILED
        transcript.attempts = 5
        await session.commit()

    response = await client.post(
        f"/api/v1/voice/{message['id']}/retry", headers=auth(users, "head")
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "attempts_exhausted"


async def test_manual_transcript_fixes_text(client: AsyncClient, users) -> None:
    """Если модель ошиблась, сотрудник вписывает текст вручную."""
    message = await _send_voice(client, users)

    fixed = await client.post(
        f"/api/v1/chats/messages/{message['id']}/transcript",
        json={"body": "Правильный текст вручную"},
        headers=auth(users, "staff"),
    )
    assert fixed.status_code == 200
    assert fixed.json()["text"] == "Правильный текст вручную"
    assert fixed.json()["is_manual"] is True
    assert fixed.json()["status"] == "done"

    # Текст дублируется в сообщении, чтобы список чата не тянул расшифровки.
    history = await client.get("/api/v1/chats", headers=auth(users, "staff2"))
    chat = history.json()[0]
    assert chat["last_message"]["transcript_text"] == "Правильный текст вручную"


async def test_cannot_fix_foreign_transcript(client: AsyncClient, users) -> None:
    message = await _send_voice(client, users, sender="staff2")
    response = await client.post(
        f"/api/v1/chats/messages/{message['id']}/transcript",
        json={"body": "Вмешательство"},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 403


async def test_voice_settings_are_readable(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/voice/settings", headers=auth(users, "head"))
    assert response.status_code == 200
    body = response.json()
    assert body["model"] in body["available_models"]
    assert body["enabled"] is False


async def test_voice_settings_reject_unknown_model(client: AsyncClient, users) -> None:
    response = await client.put(
        "/api/v1/voice/settings", json={"model": "gigantic"}, headers=auth(users, "head")
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_invalid"


async def test_voice_settings_reject_unknown_compute(client: AsyncClient, users) -> None:
    response = await client.put(
        "/api/v1/voice/settings",
        json={"compute_type": "quantum"},
        headers=auth(users, "head"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "compute_type_invalid"


async def test_voice_status_endpoint(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/voice/status", headers=auth(users, "head"))
    assert response.status_code == 200
    body = response.json()
    assert body["loaded"] is False
    assert "disk" in body
    assert "note" in body


async def test_queue_status(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/voice/queue/pending", headers=auth(users, "head"))
    assert response.status_code == 200
    assert "pending" in response.json()


async def test_staff_cannot_change_voice_settings(client: AsyncClient, users) -> None:
    response = await client.put(
        "/api/v1/voice/settings", json={"model": "small"}, headers=auth(users, "staff")
    )
    assert response.status_code == 403
