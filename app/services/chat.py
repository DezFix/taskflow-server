"""Сервис чатов: диалоги, группы, сообщения, отметки прочтения."""

from __future__ import annotations

from datetime import datetime  # noqa: F401  (используется в аннотациях ниже)
from typing import Any  # noqa: F401

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import utcnow
from app.errors import bad_request, forbidden, not_found
from app.models import (
    Attachment,
    Chat,
    ChatKind,
    ChatMember,
    Message,
    MessageStatus,
    Transcript,
    TranscriptStatus,
    User,
)
from app.security import new_id

#: В диалоге участвуют ровно двое. Ищем существующий, чтобы не плодить дубли.
DIRECT_CHAT_SIZE = 2


async def get_membership(session: AsyncSession, chat_id: str, user_id: str) -> ChatMember | None:
    return await session.scalar(
        select(ChatMember).where(
            ChatMember.chat_id == chat_id,
            ChatMember.user_id == user_id,
            ChatMember.left_at.is_(None),
        )
    )


async def load_chat_for_member(session: AsyncSession, chat_id: str, user_id: str) -> Chat:
    chat = await session.scalar(
        select(Chat)
        .where(Chat.id == chat_id)
        .options(selectinload(Chat.members).selectinload(ChatMember.user).selectinload(User.roles))
    )
    if chat is None:
        raise not_found("chat_not_found", "Чат не найден")

    member = next(
        (m for m in chat.members if m.user_id == user_id and m.left_at is None), None
    )
    if member is None:
        raise forbidden("chat_access_denied", "Вы не участник этого чата")
    return chat


async def member_ids(session: AsyncSession, chat_id: str) -> set[str]:
    rows = await session.scalars(
        select(ChatMember.user_id).where(
            ChatMember.chat_id == chat_id, ChatMember.left_at.is_(None)
        )
    )
    return set(rows)


async def find_or_create_direct_chat(
    session: AsyncSession, user: User, other_id: str
) -> Chat:
    if other_id == user.id:
        raise bad_request("self_chat", "Нельзя создать диалог с самим собой")

    other = await session.get(User, other_id)
    if other is None or not other.is_active:
        raise not_found("user_not_found", "Сотрудник не найден")

    # Ищем общий чат ровно с двумя активными участниками.
    candidates = await session.scalars(
        select(Chat).where(Chat.kind == ChatKind.DIRECT, Chat.is_archived.is_(False))
    )
    for chat in candidates:
        active = [m.user_id for m in chat.members if m.left_at is None]
        if len(active) == DIRECT_CHAT_SIZE and set(active) == {user.id, other_id}:
            return chat

    chat = Chat(
        id=new_id(),
        kind=ChatKind.DIRECT,
        created_by_id=user.id,
    )
    session.add(chat)
    await session.flush()

    for member_id in (user.id, other_id):
        session.add(
            ChatMember(
                id=new_id(),
                chat_id=chat.id,
                user_id=member_id,
                last_read_seq=0,
            )
        )
    await session.flush()
    return chat


async def create_group_chat(
    session: AsyncSession, user: User, title: str, member_ids_list: list[str]
) -> Chat:
    unique_ids = list(dict.fromkeys(member_ids_list))
    if user.id not in unique_ids:
        unique_ids.append(user.id)

    users = (
        await session.scalars(
            select(User).where(User.id.in_(unique_ids), User.is_active.is_(True))
        )
    ).all()
    if len(users) != len(unique_ids):
        raise bad_request("member_invalid", "Некоторые сотрудники не найдены или отключены")

    chat = Chat(
        id=new_id(),
        kind=ChatKind.GROUP,
        title=title.strip(),
        created_by_id=user.id,
    )
    session.add(chat)
    await session.flush()

    for member in users:
        session.add(
            ChatMember(
                id=new_id(),
                chat_id=chat.id,
                user_id=member.id,
                is_admin=(member.id == user.id),
                last_read_seq=0,
            )
        )
    await session.flush()
    return chat


async def next_message_seq(session: AsyncSession, chat_id: str) -> int:
    current = await session.scalar(
        select(func.max(Message.seq)).where(Message.chat_id == chat_id)
    )
    return (current or 0) + 1


async def add_text_message(
    session: AsyncSession,
    chat: Chat,
    sender: User,
    body: str,
    reply_to_id: str | None = None,
) -> Message:
    if reply_to_id:
        parent = await session.get(Message, reply_to_id)
        if parent is None or parent.chat_id != chat.id:
            raise bad_request("reply_invalid", "Сообщение, на которое вы отвечаете, не найдено")

    message = Message(
        id=new_id(),
        chat_id=chat.id,
        sender_id=sender.id,
        body=body.strip(),
        kind="text",
        seq=await next_message_seq(session, chat.id),
        status=MessageStatus.DELIVERED,
        reply_to_id=reply_to_id,
    )
    session.add(message)
    await session.flush()
    await mark_delivered(session, chat.id, exclude_user_id=sender.id)
    return message


async def add_attachment_message(
    session: AsyncSession,
    chat: Chat,
    sender: User,
    *,
    attachment: Attachment,
    body: str | None = None,
    is_voice: bool = False,
    duration_sec: float | None = None,
) -> Message:
    """Сообщение с файлом. Для голосового сразу создаётся задача расшифровки."""
    message = Message(
        id=new_id(),
        chat_id=chat.id,
        sender_id=sender.id,
        body=(body or "").strip() or None,
        kind="voice" if is_voice else ("image" if attachment.kind == "image" else "file"),
        attachment_id=attachment.id,
        seq=await next_message_seq(session, chat.id),
        status=MessageStatus.DELIVERED,
        voice_duration_sec=duration_sec if is_voice else None,
    )
    session.add(message)
    await session.flush()
    await mark_delivered(session, chat.id, exclude_user_id=sender.id)

    if is_voice:
        from app.services.voice_jobs import submit_transcription

        await submit_transcription(
            session, message=message, attachment=attachment, duration_sec=duration_sec
        )

    return message


async def mark_delivered(session: AsyncSession, chat_id: str, exclude_user_id: str | None) -> None:
    rows = await session.scalars(
        select(Message).where(
            Message.chat_id == chat_id,
            Message.status == MessageStatus.SENT,
        )
    )
    changed = False
    for message in rows:
        if exclude_user_id and message.sender_id == exclude_user_id:
            continue
        message.status = MessageStatus.DELIVERED
        changed = True
    if changed:
        await session.flush()


async def mark_read(session: AsyncSession, chat: Chat, user: User, seq: int) -> list[str]:
    """Отмечает прочитанными сообщения до seq. Возвращает id прочитанных."""
    member = await get_membership(session, chat.id, user.id)
    if member is None:
        raise forbidden("chat_access_denied", "Вы не участник этого чата")

    unread = (
        await session.scalars(
            select(Message).where(
                Message.chat_id == chat.id,
                Message.seq > member.last_read_seq,
                Message.seq <= seq,
                Message.sender_id != user.id,
                Message.deleted_at.is_(None),
            )
        )
    ).all()

    if not unread:
        return []

    read_ids = [m.id for m in unread]
    for message in unread:
        message.status = MessageStatus.READ

    member.last_read_seq = max(member.last_read_seq, seq)
    await session.flush()
    return read_ids


async def unread_count(session: AsyncSession, chat_id: str, user: User) -> int:
    member = await get_membership(session, chat_id, user.id)
    if member is None:
        return 0
    result = await session.scalar(
        select(func.count(Message.id)).where(
            Message.chat_id == chat_id,
            Message.seq > member.last_read_seq,
            Message.sender_id != user.id,
            Message.deleted_at.is_(None),
        )
    )
    return int(result or 0)


async def list_chats(session: AsyncSession, user: User) -> list[Chat]:
    rows = await session.scalars(
        select(Chat)
        .join(ChatMember, ChatMember.chat_id == Chat.id)
        .where(
            ChatMember.user_id == user.id,
            ChatMember.left_at.is_(None),
            Chat.is_archived.is_(False),
        )
        .options(
            selectinload(Chat.members).selectinload(ChatMember.user).selectinload(User.roles)
        )
    )
    return list(rows)


async def last_message_for(session: AsyncSession, chat_id: str) -> Message | None:
    return await session.scalar(
        select(Message)
        .where(Message.chat_id == chat_id, Message.deleted_at.is_(None))
        .options(
            selectinload(Message.sender).selectinload(User.roles),
            selectinload(Message.attachment),
            selectinload(Message.transcript),
        )
        .order_by(Message.seq.desc())
        .limit(1)
    )


async def add_group_members(session: AsyncSession, chat: Chat, user_ids: list[str]) -> list[str]:
    existing = {m.user_id for m in chat.members if m.left_at is None}
    added: list[str] = []

    users = (
        await session.scalars(
            select(User).where(User.id.in_(user_ids), User.is_active.is_(True))
        )
    ).all()
    for member in users:
        if member.id in existing:
            continue
        # Ушедший участник возвращается, а не дублируется.
        rejoin = next((m for m in chat.members if m.user_id == member.id), None)
        if rejoin is not None:
            rejoin.left_at = None
        else:
            session.add(
                ChatMember(id=new_id(), chat_id=chat.id, user_id=member.id, last_read_seq=0)
            )
        added.append(member.id)

    if added:
        await session.flush()
    return added


async def remove_group_members(session: AsyncSession, chat: Chat, user_ids: list[str]) -> int:
    members = [m for m in chat.members if m.user_id in user_ids and m.left_at is None]
    for member in members:
        member.left_at = utcnow()
    if members:
        await session.flush()
    return len(members)


def other_member(chat: Chat, user_id: str) -> ChatMember | None:
    for member in chat.members:
        if member.user_id != user_id and member.left_at is None:
            return member
    return None


def display_title(chat: Chat, user_id: str) -> str | None:
    """Для диалога показываем имя собеседника, для группы — название."""
    if chat.kind == ChatKind.GROUP:
        return chat.title
    counterpart = other_member(chat, user_id)
    return counterpart.user.full_name if counterpart else None


async def delete_message(session: AsyncSession, message: Message, user: User) -> None:
    """Удаление у всех: тело скрывается, вложение остаётся в истории задач."""
    now = utcnow()
    message.deleted_at = now
    message.body = None
    message.status = MessageStatus.SENT
    if message.attachment_id:
        message.attachment_id = None
    await session.flush()


async def edit_message(session: AsyncSession, message: Message, body: str) -> None:
    message.body = body.strip()
    message.edited_at = utcnow()
    await session.flush()


async def transcript_for(session: AsyncSession, message_id: str) -> Transcript | None:
    return await session.scalar(select(Transcript).where(Transcript.message_id == message_id))


def transcript_payload(transcript: Transcript) -> dict:
    return {
        "id": transcript.id,
        "status": transcript.status.value,
        "text": transcript.text,
        "language": transcript.language,
        "model_name": transcript.model_name,
        "duration_sec": transcript.duration_sec,
        "confidence": transcript.confidence,
        "error": transcript.error,
        "is_manual": transcript.is_manual,
    }


def pending_transcripts_query():  # noqa: ANN202
    return select(Transcript).where(
        Transcript.status.in_([TranscriptStatus.PENDING, TranscriptStatus.RUNNING])
    )


def messages_for_notification(chat_id: str) -> str:  # pragma: no cover
    return f"Новое сообщение в чате {chat_id}"
