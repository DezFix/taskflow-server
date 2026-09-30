"""Чат: диалоги, группы, сообщения, отметки прочтения."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.deps import SessionDep, has_perm, require_perm
from app.errors import bad_request, forbidden, not_found
from app.models import Chat, ChatMember, Message, User
from app.realtime import (
    EVENT_CHAT_UPDATED,
    EVENT_MESSAGE_CREATED,
    EVENT_MESSAGE_DELETED,
    EVENT_MESSAGE_READ,
    EVENT_MESSAGE_UPDATED,
    hub,
)
from app.schemas import (
    ChatDetailOut,
    ChatMemberOut,
    ChatOut,
    DirectChatCreate,
    GroupChatCreate,
    GroupUpdate,
    MarkReadRequest,
    MessageEdit,
    MessageOut,
    OkMessage,
    Page,
    TextMessageCreate,
    TranscriptOut,
)
from app.serializers import message_out, user_brief
from app.services import audit as audit_service
from app.services import chat as chat_service
from app.services import files as files_service
from app.services import voice_engine

router = APIRouter(prefix="/chats", tags=["chat"])


async def _chat_view(
    session: SessionDep, chat: Chat, user: User
) -> ChatOut:
    member = next(
        (m for m in chat.members if m.user_id == user.id and m.left_at is None), None
    )
    last = await chat_service.last_message_for(session, chat.id)
    unread = await chat_service.unread_count(session, chat.id, user)
    return ChatOut(
        id=chat.id,
        kind=chat.kind.value,
        title=chat_service.display_title(chat, user.id),
        avatar_url=(
            f"/api/v1/files/{chat.avatar_file_id}/download" if chat.avatar_file_id else None
        ),
        members=[user_brief(m.user) for m in chat.members if m.left_at is None],
        last_message=message_out(last) if last else None,
        last_message_at=last.created_at if last else None,
        unread_count=unread,
        is_admin=bool(member and member.is_admin),
        is_muted=bool(member and member.is_muted),
        created_at=chat.created_at,
    )


@router.get(
    "",
    response_model=list[ChatOut],
    summary="Список чатов",
    description="Отсортирован по времени последнего сообщения. Содержит счётчики непрочитанного.",
)
async def list_chats(
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> list[ChatOut]:
    chats = await chat_service.list_chats(session, user)
    views = [await _chat_view(session, chat, user) for chat in chats]
    views.sort(
        key=lambda c: c.last_message_at or c.created_at,
        reverse=True,
    )
    return views


@router.get("/unread/total", response_model=dict, summary="Всего непрочитанных")
async def unread_total(
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> dict:
    total = await session.scalar(
        select(func.count(Message.id))
        .join(ChatMember, ChatMember.chat_id == Message.chat_id)
        .where(
            ChatMember.user_id == user.id,
            ChatMember.left_at.is_(None),
            Message.seq > ChatMember.last_read_seq,
            Message.sender_id != user.id,
            Message.deleted_at.is_(None),
        )
    )
    return {"unread": int(total or 0)}


@router.post(
    "/direct",
    response_model=ChatOut,
    summary="Открыть личный диалог",
    description="Возвращает существующий диалог или создаёт новый — дубликатов не будет.",
)
async def open_direct_chat(
    payload: DirectChatCreate,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> ChatOut:
    chat = await chat_service.find_or_create_direct_chat(session, user, payload.user_id)
    chat = await chat_service.load_chat_for_member(session, chat.id, user.id)
    return await _chat_view(session, chat, user)


@router.post(
    "/groups",
    response_model=ChatOut,
    status_code=201,
    summary="Создать группу",
)
async def create_group(
    payload: GroupChatCreate,
    session: SessionDep,
    user: User = Depends(require_perm("chat.group_create")),
) -> ChatOut:
    chat = await chat_service.create_group_chat(
        session, user, payload.title, payload.member_ids
    )
    chat = await chat_service.load_chat_for_member(session, chat.id, user.id)

    await hub.send_to_users(
        {m.user_id for m in chat.members},
        EVENT_CHAT_UPDATED,
        {"chat_id": chat.id, "action": "created"},
    )
    await audit_service.log_action(
        session,
        user=user,
        action="chat.group_create",
        entity_type="chat",
        entity_id=chat.id,
        details={"title": chat.title, "members": len(chat.members)},
    )
    return await _chat_view(session, chat, user)


@router.get("/{chat_id}", response_model=ChatDetailOut, summary="Детали чата")
async def get_chat(
    chat_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> ChatDetailOut:
    chat = await chat_service.load_chat_for_member(session, chat_id, user.id)
    base = await _chat_view(session, chat, user)
    return ChatDetailOut(
        **base.model_dump(),
        members_detail=[
            ChatMemberOut(
                user=user_brief(m.user),
                is_admin=m.is_admin,
                last_read_seq=m.last_read_seq,
                joined_at=m.created_at,
            )
            for m in chat.members
            if m.left_at is None
        ],
        created_by_id=chat.created_by_id,
    )


@router.patch(
    "/{chat_id}",
    response_model=ChatOut,
    summary="Изменить группу",
    description="Переименовать, добавить или убрать участников.",
)
async def update_group(
    chat_id: str,
    payload: GroupUpdate,
    session: SessionDep,
    user: User = Depends(require_perm("chat.group")),
) -> ChatOut:
    chat = await chat_service.load_chat_for_member(session, chat_id, user.id)
    if chat.is_direct:
        raise bad_request("direct_chat_immutable", "Личный диалог нельзя изменить")

    member = next((m for m in chat.members if m.user_id == user.id), None)
    is_group_admin = member is not None and (member.is_admin or chat.created_by_id == user.id)
    if not is_group_admin and not has_perm(user, "chat.group_create"):
        raise forbidden("group_admin_only", "Изменять группу может только её администратор")

    data = payload.model_dump(exclude_unset=True)
    if data.get("title"):
        chat.title = data["title"].strip()
    if data.get("add_member_ids"):
        await chat_service.add_group_members(session, chat, data["add_member_ids"])
    if data.get("remove_member_ids"):
        remaining = {m.user_id for m in chat.members if m.left_at is None}
        if chat.created_by_id in data["remove_member_ids"]:
            raise bad_request("cannot_remove_owner", "Нельзя удалить создателя группы")
        if len(remaining - set(data["remove_member_ids"])) < 2:
            raise bad_request("group_too_small", "В группе должно остаться хотя бы двое")
        await chat_service.remove_group_members(session, chat, data["remove_member_ids"])

    if data.get("member_ids"):
        target = list(dict.fromkeys(data["member_ids"]))
        current = {m.user_id for m in chat.members if m.left_at is None}
        await chat_service.add_group_members(session, chat, [u for u in target if u not in current])
        await chat_service.remove_group_members(
            session, chat, [u for u in current if u not in target]
        )

    await session.flush()
    chat = await chat_service.load_chat_for_member(session, chat.id, user.id)
    await hub.send_to_users(
        {m.user_id for m in chat.members},
        EVENT_CHAT_UPDATED,
        {"chat_id": chat.id, "action": "updated"},
    )
    return await _chat_view(session, chat, user)


@router.get(
    "/{chat_id}/messages",
    response_model=Page[MessageOut],
    summary="История сообщений",
    description="Курсорная пагинация от новых к старым.",
)
async def list_messages(
    chat_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
    limit: int = Query(50, ge=1, le=200),
    before_seq: int | None = Query(default=None, description="Сообщения старше этой номера"),
) -> Page[MessageOut]:
    await chat_service.load_chat_for_member(session, chat_id, user.id)

    stmt = (
        select(Message)
        .where(Message.chat_id == chat_id)
        .options(
            selectinload(Message.sender).selectinload(User.roles),
            selectinload(Message.attachment),
            selectinload(Message.transcript),
        )
    )
    if before_seq:
        stmt = stmt.where(Message.seq < before_seq)

    rows = list((await session.scalars(stmt.order_by(Message.seq.desc()).limit(limit + 1))).all())
    has_more = len(rows) > limit
    items = rows[:limit]

    return Page[MessageOut](
        items=[message_out(message) for message in items],
        next_cursor=str(items[-1].seq) if has_more and items else None,
        has_more=has_more,
    )


@router.post(
    "/{chat_id}/messages",
    response_model=MessageOut,
    status_code=201,
    summary="Отправить сообщение",
    description=(
        "Текст, файл или голосовое. Голосовое принимается отдельным запросом "
        "POST /chats/{id}/voice — здесь только текст и файлы."
    ),
)
async def send_message(
    chat_id: str,
    payload: TextMessageCreate,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> MessageOut:
    chat = await chat_service.load_chat_for_member(session, chat_id, user.id)
    message = await chat_service.add_text_message(
        session, chat, user, payload.body, payload.reply_to_id
    )
    message = await _reload(session, message)

    recipients = {m.user_id for m in chat.members if m.left_at is None}
    await hub.send_to_users(
        recipients,
        EVENT_MESSAGE_CREATED,
        message_out(message).model_dump(),
    )
    return message_out(message)


@router.post(
    "/{chat_id}/files",
    response_model=MessageOut,
    status_code=201,
    summary="Отправить файл в чат",
    description="Фото, документ или изображение из отчёта.",
)
async def send_file(
    session: SessionDep,
    chat_id: str,
    file: Annotated[UploadFile, File(description="Файл для отправки")],
    body: Annotated[str | None, Form(description="Подпись к файлу")] = None,
    user: User = Depends(require_perm("files.upload", "chat.direct")),
) -> MessageOut:
    chat = await chat_service.load_chat_for_member(session, chat_id, user.id)
    data = await file.read()
    attachment = await files_service.save_upload(
        data,
        file.filename or "file",
        user,
        mime_type=file.content_type,
    )
    await file.close()
    session.add(attachment)
    await session.flush()

    message = await chat_service.add_attachment_message(
        session, chat, user, attachment=attachment, body=body
    )
    message = await _reload(session, message)

    await hub.send_to_users(
        {m.user_id for m in chat.members if m.left_at is None},
        EVENT_MESSAGE_CREATED,
        message_out(message).model_dump(),
    )
    return message_out(message)


@router.post(
    "/{chat_id}/voice",
    response_model=MessageOut,
    status_code=201,
    summary="Отправить голосовое сообщение",
    description=(
        "Аудио сохраняется и сразу появляется в чате, а расшифровка выполняется "
        "на сервере в фоне. Текст придёт событием 'transcript.ready'."
    ),
)
async def send_voice(
    session: SessionDep,
    chat_id: str,
    file: Annotated[
        UploadFile, File(description="Аудио в одном из форматов: m4a, ogg, mp3, wav, opus")
    ],
    duration_sec: Annotated[
        float | None, Form(description="Длительность записи в секундах")
    ] = None,
    user: User = Depends(require_perm("chat.direct")),
) -> MessageOut:
    if not get_settings().voice_enabled and not has_perm(user, "voice.transcribe"):
        # Голосовое принимаем всегда: даже без распознавания это способ
        # передать аудио. Право voice.transcribe даёт доступ к настройке.
        pass

    chat = await chat_service.load_chat_for_member(session, chat_id, user.id)
    data = await file.read()
    if not data:
        raise bad_request("file_empty", "Аудиофайл пуст")

    attachment = await files_service.save_upload(
        data,
        file.filename or "voice.m4a",
        user,
        mime_type=file.content_type or "audio/mp4",
        meta={"duration_sec": duration_sec} if duration_sec else None,
    )
    await file.close()
    session.add(attachment)
    await session.flush()

    duration = duration_sec or (attachment.meta or {}).get("duration_sec")
    message = await chat_service.add_attachment_message(
        session,
        chat,
        user,
        attachment=attachment,
        is_voice=True,
        duration_sec=duration,
    )
    message = await _reload(session, message)

    await hub.send_to_users(
        {m.user_id for m in chat.members if m.left_at is None},
        EVENT_MESSAGE_CREATED,
        message_out(message).model_dump(),
    )
    return message_out(message)


async def _reload(session: SessionDep, message: Message) -> Message:
    return await session.scalar(
        select(Message)
        .where(Message.id == message.id)
        .options(
            selectinload(Message.sender).selectinload(User.roles),
            selectinload(Message.attachment),
            selectinload(Message.transcript),
        )
    )


@router.post("/{chat_id}/read", response_model=OkMessage, summary="Отметить прочитанным")
async def mark_read(
    chat_id: str,
    payload: MarkReadRequest,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> OkMessage:
    chat = await chat_service.load_chat_for_member(session, chat_id, user.id)
    last_seq = await session.scalar(
        select(func.max(Message.seq)).where(Message.chat_id == chat_id)
    )
    read_ids = await chat_service.mark_read(
        session, chat, user, payload.seq or (last_seq or 0)
    )

    if read_ids:
        await hub.send_to_users(
            {m.user_id for m in chat.members if m.left_at is None},
            EVENT_MESSAGE_READ,
            {
                "chat_id": chat_id,
                "user_id": user.id,
                "message_ids": read_ids,
                "up_to_seq": payload.seq or (last_seq or 0),
            },
        )
    return OkMessage(detail=f"Отмечено прочитанными: {len(read_ids)}")


@router.patch(
    "/messages/{message_id}",
    response_model=MessageOut,
    summary="Изменить своё сообщение",
)
async def edit_message(
    message_id: str,
    payload: MessageEdit,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> MessageOut:
    message = await session.get(Message, message_id)
    if message is None or message.deleted_at is not None:
        raise not_found("message_not_found", "Сообщение не найдено")
    if message.sender_id != user.id:
        raise forbidden("not_your_message", "Можно править только свои сообщения")

    await chat_service.edit_message(session, message, payload.body)
    message = await _reload(session, message)
    await hub.send_to_users(
        await chat_service.member_ids(session, message.chat_id),
        EVENT_MESSAGE_UPDATED,
        message_out(message).model_dump(),
    )
    return message_out(message)


@router.delete(
    "/messages/{message_id}",
    response_model=OkMessage,
    summary="Удалить сообщение",
    description="Удаление у всех. Вложение остаётся в истории задач, если было привязано.",
)
async def delete_message(
    message_id: str,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> OkMessage:
    message = await session.get(Message, message_id)
    if message is None:
        raise not_found("message_not_found", "Сообщение не найден")

    is_admin = await session.scalar(
        select(func.count(ChatMember.id)).where(
            ChatMember.chat_id == message.chat_id,
            ChatMember.user_id == user.id,
            ChatMember.is_admin.is_(True),
        )
    )
    if message.sender_id != user.id and not is_admin and not has_perm(user, "settings.manage_roles"):
        raise forbidden("not_your_message", "Можно удалять только свои сообщения")

    chat_id = message.chat_id
    await chat_service.delete_message(session, message, user)
    await hub.send_to_users(
        await chat_service.member_ids(session, chat_id),
        EVENT_MESSAGE_DELETED,
        {"message_id": message_id, "chat_id": chat_id},
    )
    return OkMessage(detail="Сообщение удалено")


@router.post(
    "/messages/{message_id}/transcript",
    response_model=TranscriptOut,
    summary="Исправить расшифровку вручную",
    description=(
        "Если Whisper ошибся, сотрудник может вписать текст сам. "
        "Ручная правка помечается и больше не перезаписывается."
    ),
)
async def fix_transcript(
    message_id: str,
    payload: TextMessageCreate,
    session: SessionDep,
    user: User = Depends(require_perm("chat.direct")),
) -> TranscriptOut:
    from app.services import voice_jobs

    message = await session.get(Message, message_id)
    if message is None:
        raise not_found("message_not_found", "Сообщение не найден")
    if message.sender_id != user.id and not has_perm(user, "settings.manage_roles"):
        raise forbidden("not_your_message", "Можно править только свои сообщения")

    transcript = await chat_service.transcript_for(session, message_id)
    if transcript is None:
        raise not_found("transcript_missing", "У сообщения нет записи распознавания")

    await voice_jobs.save_manual_transcript(session, transcript, message, payload.body)

    await hub.send_to_users(
        await chat_service.member_ids(session, message.chat_id),
        "transcript.ready",
        {
            "message_id": message_id,
            "chat_id": message.chat_id,
            "transcript": chat_service.transcript_payload(transcript),
        },
    )
    return TranscriptOut.model_validate(transcript)


@router.get("/voice/capabilities", summary="Что умеет сервер по голосу")
async def voice_capabilities(
    _: User = Depends(require_perm("chat.direct")),
) -> dict:
    from app.services.voice_jobs import pending_count

    return {
        "enabled": get_settings().voice_enabled,
        "model": get_settings().voice_model,
        "language": get_settings().voice_language,
        "formats": voice_engine.supported_audio_extensions(),
        "pending": pending_count(),
        "note": voice_engine.warmup_note(),
    }
