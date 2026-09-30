"""Тесты загрузки файлов: проверка типа, размера, доступ, превью."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import TEST_PNG

pytestmark = pytest.mark.asyncio


def auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {users.tokens[role]}"}


async def test_upload_png(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "photo.png"
    assert body["size_bytes"] == len(TEST_PNG)
    assert body["kind"] == "image"
    assert body["checksum"]
    assert body["url"].endswith("/download")


async def test_upload_rejects_executable(client: AsyncClient, users) -> None:
    """Переименованный исполняемый файл не должен приниматься."""
    response = await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", b"MZ\x90\x00\x03" + b"\x00" * 100, "image/png")},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "content_not_allowed"


async def test_upload_rejects_unknown_extension(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/files",
        files={"file": ("script.sh", b"#!/bin/sh\necho hi\n", "text/plain")},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "extension_not_allowed"


async def test_upload_rejects_extensionless_file(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/files",
        files={"file": ("noextension", b"data", "application/octet-stream")},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "extension_required"


async def test_upload_rejects_empty_file(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/files",
        files={"file": ("empty.png", b"", "image/png")},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "file_empty"


async def test_upload_rejects_oversized_file(client: AsyncClient, users) -> None:
    """Лимит в тестах — 2 МБ."""
    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)
    response = await client.post(
        "/api/v1/files",
        files={"file": ("big.png", oversized, "image/png")},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


async def test_filename_path_traversal_is_stripped(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/files",
        files={"file": ("../../etc/passwd.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 201
    name = response.json()["name"]
    assert "/" not in name
    assert ".." not in name


async def test_download_own_file(client: AsyncClient, users) -> None:
    uploaded = await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )
    file_id = uploaded.json()["id"]

    download = await client.get(
        f"/api/v1/files/{file_id}/download", headers=auth(users, "staff")
    )
    assert download.status_code == 200
    assert download.content == TEST_PNG
    assert "image/png" in download.headers["content-type"]


async def test_foreign_file_requires_permission(client: AsyncClient, users) -> None:
    """Чужой файл без права files.view_all недоступен — это 403, а не 400."""
    uploaded = await client.post(
        "/api/v1/files",
        files={"file": ("secret.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )
    file_id = uploaded.json()["id"]

    denied = await client.get(
        f"/api/v1/files/{file_id}/download", headers=auth(users, "staff2")
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "file_access_denied"

    # С правами files.view_all — доступ есть.
    allowed = await client.get(
        f"/api/v1/files/{file_id}/download", headers=auth(users, "head")
    )
    assert allowed.status_code == 200


async def test_preview_returns_image(client: AsyncClient, users) -> None:
    """Превью отдаётся картинкой даже без сгенерированного thumb."""
    uploaded = await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )
    file_id = uploaded.json()["id"]

    preview = await client.get(
        f"/api/v1/files/{file_id}/preview", headers=auth(users, "staff")
    )
    assert preview.status_code == 200
    # Либо сжатое превью, либо оригинал — оба читаются как PNG.
    assert preview.content.startswith(b"\x89PNG")
    assert preview.headers["x-content-type-options"] == "nosniff"


async def test_file_content_type_is_not_guessed_from_request(
    client: AsyncClient, users
) -> None:
    """Сервер отдаёт тип по фактически сохранённому расширению."""
    uploaded = await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "text/html")},
        headers=auth(users, "staff"),
    )
    file_id = uploaded.json()["id"]

    download = await client.get(
        f"/api/v1/files/{file_id}/download", headers=auth(users, "staff")
    )
    assert download.status_code == 200
    assert "image/png" in download.headers["content-type"]
    assert "text/html" not in download.headers["content-type"]


async def test_file_metadata(client: AsyncClient, users) -> None:
    uploaded = await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )
    file_id = uploaded.json()["id"]
    meta = await client.get(f"/api/v1/files/{file_id}", headers=auth(users, "staff"))
    assert meta.status_code == 200
    assert meta.json()["id"] == file_id


async def test_missing_file_returns_404(client: AsyncClient, users) -> None:
    response = await client.get(
        "/api/v1/files/nonexistent/download", headers=auth(users, "head")
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "file_not_found"


async def test_file_attached_to_task_as_work_report(client: AsyncClient, users) -> None:
    """Сценарий целиком: сотрудник загружает фото и прикладывает как отчёт."""
    task = await client.post(
        "/api/v1/tasks",
        json={"title": "Починить МФУ", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    task_id = task.json()["id"]

    uploaded = await client.post(
        "/api/v1/files",
        files={"file": ("report.png", TEST_PNG, "image/png")},
        data={"task_id": task_id},
        headers=auth(users, "staff"),
    )
    assert uploaded.status_code == 201
    assert uploaded.json()["task_id"] == task_id
    file_id = uploaded.json()["id"]

    comment = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "Заменил узел подачи", "attachment_ids": [file_id], "is_work_report": True},
        headers=auth(users, "staff"),
    )
    assert comment.status_code == 201
    assert len(comment.json()["attachments"]) == 1
    assert comment.json()["attachments"][0]["id"] == file_id

    task_files = await client.get(
        f"/api/v1/files/attachments/for-task/{task_id}", headers=auth(users, "head")
    )
    assert len(task_files.json()) == 1

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(users, "head"))
    assert detail.json()["status"] == "review"
    assert detail.json()["attachments_count"] >= 1
    assert len(detail.json()["comments"][0]["attachments"]) == 1


async def test_comment_with_unknown_attachment_rejected(client: AsyncClient, users) -> None:
    task = await client.post(
        "/api/v1/tasks", json={"title": "Задача"}, headers=auth(users, "head")
    )
    response = await client.post(
        f"/api/v1/tasks/{task.json()['id']}/comments",
        json={"body": "Смотри", "attachment_ids": ["несуществующий"]},
        headers=auth(users, "head"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "attachment_missing"


async def test_attachment_of_another_user_is_rejected(
    client: AsyncClient, users
) -> None:
    """Нельзя прикрепить к своей задаче файл, загруженный другим сотрудником.

    Иначе сотрудник присвоил бы чужой фотоотчёт: файл стал бы виден
    в его задаче и в списке вложений.
    """
    task = await client.post(
        "/api/v1/tasks",
        json={"title": "Моя задача", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    task_id = task.json()["id"]

    foreign = await client.post(
        "/api/v1/files",
        files={"file": ("staff2.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff2"),
    )
    assert foreign.status_code == 201

    response = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "Прикрепляю", "attachment_ids": [foreign.json()["id"]]},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "attachment_not_yours"


async def test_delete_file(client: AsyncClient, users) -> None:
    uploaded = await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )
    file_id = uploaded.json()["id"]

    deleted = await client.delete(f"/api/v1/files/{file_id}", headers=auth(users, "staff"))
    assert deleted.status_code == 200

    gone = await client.get(f"/api/v1/files/{file_id}/download", headers=auth(users, "staff"))
    assert gone.status_code == 404


async def test_storage_usage(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )
    response = await client.get("/api/v1/files/storage/usage", headers=auth(users, "head"))
    assert response.status_code == 200
    assert response.json()["files"] >= 1
    assert response.json()["max_upload_mb"] == 2


async def test_staff_cannot_upload_without_permission(client: AsyncClient, users) -> None:
    """Роль без files.upload не может загружать файлы."""
    role = await client.post(
        "/api/v1/roles",
        json={"title": "Наблюдатель", "permissions": ["tasks.view"]},
        headers=auth(users, "admin"),
    )
    user = await client.post(
        "/api/v1/users",
        json={"username": "watcher", "full_name": "Наблюдатель", "role_ids": [role.json()["id"]]},
        headers=auth(users, "admin"),
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "watcher", "password": user.json()["temporary_password"]},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        headers=headers,
    )
    assert response.status_code == 403
