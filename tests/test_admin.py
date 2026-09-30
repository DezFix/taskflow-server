"""Тесты администрирования: журнал действий, бэкапы, обслуживание."""

from __future__ import annotations

import zipfile

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.services.backup import backup_path
from tests.conftest import TEST_PNG

pytestmark = pytest.mark.asyncio


def auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {users.tokens[role]}"}


async def test_audit_records_user_creation(client: AsyncClient, users) -> None:
    roles = await client.get("/api/v1/roles", headers=auth(users, "head"))
    staff_role = next(r for r in roles.json() if r["key"] == "Сотрудник")

    await client.post(
        "/api/v1/users",
        json={"username": "audit_user", "full_name": "Под аудитом", "role_ids": [staff_role["id"]]},
        headers=auth(users, "head"),
    )

    audit = await client.get("/api/v1/admin/audit", headers=auth(users, "head"))
    assert audit.status_code == 200
    body = audit.json()
    assert body["total"] >= 1
    action = body["items"][0]["action"]
    assert action == "user.create"
    assert body["items"][0]["entity_type"] == "user"
    assert body["items"][0]["created_at"]


async def test_audit_records_task_lifecycle(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks", json={"title": "Аудит задачи"}, headers=auth(users, "head")
    )
    task_id = created.json()["id"]
    await client.post(
        f"/api/v1/tasks/{task_id}/status",
        json={"status": "done"},
        headers=auth(users, "head"),
    )

    audit = await client.get(
        "/api/v1/admin/audit?entity_type=task", headers=auth(users, "head")
    )
    actions = [item["action"] for item in audit.json()["items"]]
    assert "task.create" in actions


async def test_audit_filter_by_action(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/positions", json={"title": "Для аудита"}, headers=auth(users, "head")
    )
    # Создание должности в журнал не пишется, но фильтр работать должен.
    audit = await client.get(
        "/api/v1/admin/audit?action=position.create", headers=auth(users, "head")
    )
    assert audit.status_code == 200
    assert audit.json()["total"] == 0


async def test_audit_actions_catalog(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/positions", json={"title": "Каталог"}, headers=auth(users, "head")
    )
    await client.post("/api/v1/tasks", json={"title": "Задача"}, headers=auth(users, "head"))

    response = await client.get("/api/v1/admin/audit/actions", headers=auth(users, "head"))
    assert response.status_code == 200
    assert "task.create" in response.json()


async def test_backup_creation_and_listing(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/files",
        files={"file": ("photo.png", TEST_PNG, "image/png")},
        headers=auth(users, "staff"),
    )

    created = await client.post("/api/v1/admin/backup", headers=auth(users, "head"))
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["filename"].endswith(".zip")
    assert body["includes_database"] is True
    assert body["files"] >= 1
    assert body["size_bytes"] > 0

    listed = await client.get("/api/v1/admin/backups", headers=auth(users, "head"))
    assert listed.status_code == 200
    assert any(item["id"] == body["id"] for item in listed.json())


async def test_backup_without_files(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/admin/backup?include_files=false", headers=auth(users, "head")
    )
    assert created.status_code == 200
    assert created.json()["files"] == 0


async def test_backup_restore(client: AsyncClient, users) -> None:
    created = await client.post("/api/v1/admin/backup", headers=auth(users, "head"))
    backup_id = created.json()["id"]

    restored = await client.post(
        f"/api/v1/admin/backups/{backup_id}/restore", headers=auth(users, "head")
    )
    assert restored.status_code == 200
    assert "files_restored" in restored.json()


async def test_backup_delete(client: AsyncClient, users) -> None:
    created = await client.post("/api/v1/admin/backup", headers=auth(users, "head"))
    backup_id = created.json()["id"]

    deleted = await client.delete(
        f"/api/v1/admin/backups/{backup_id}", headers=auth(users, "head")
    )
    assert deleted.status_code == 200

    listed = await client.get("/api/v1/admin/backups", headers=auth(users, "head"))
    assert all(item["id"] != backup_id for item in listed.json())


async def test_missing_backup_returns_404(client: AsyncClient, users) -> None:
    response = await client.delete(
        "/api/v1/admin/backups/nonexistent", headers=auth(users, "head")
    )
    assert response.status_code == 404


async def test_backup_id_cannot_escape_directory(client: AsyncClient, users) -> None:
    """В идентификаторе копии нельзя пройти в другие каталоги файловой системы."""
    response = await client.delete(
        "/api/v1/admin/backups/..%2F..%2Fsecret", headers=auth(users, "head")
    )
    assert response.status_code in (404, 400)


async def test_backup_restore_rejects_path_traversal(client: AsyncClient, users) -> None:
    """Запись вида files/../ не должна уйти писать за пределы хранилища.

    Архив бэкапа берётся с диска, поэтому злоумышленник, подложивший
    свой файл в папку бэкапов, иначе записал бы что угодно на диск.
    """
    created = await client.post("/api/v1/admin/backup", headers=auth(users, "head"))
    backup_id = created.json()["id"]

    settings = get_settings()
    outside = settings.storage_path.parent / "escaped.txt"
    outside.unlink(missing_ok=True)

    # Заменяем архив настоящим, но с вредоносной записью внутри.
    archive_path = backup_path(backup_id)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("files/../escaped.txt", "не должно записаться")
        archive.writestr("files/normal.txt", "обычный файл")

    response = await client.post(
        f"/api/v1/admin/backups/{backup_id}/restore", headers=auth(users, "head")
    )
    assert response.status_code == 200
    assert not outside.exists(), "файл записан за пределы папки данных"

    assert (settings.storage_path / "normal.txt").exists()


async def test_staff_cannot_create_backup(client: AsyncClient, users) -> None:
    response = await client.post("/api/v1/admin/backup", headers=auth(users, "staff"))
    assert response.status_code == 403


async def test_system_info(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/admin/system/info", headers=auth(users, "head"))
    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "sqlite"
    assert body["database_is_sqlite"] is True
    assert body["max_upload_mb"] == 2
    assert "python" in body


async def test_maintenance_cleanup(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/admin/maintenance/cleanup", headers=auth(users, "head")
    )
    assert response.status_code == 200
    assert "removed" in response.json()
