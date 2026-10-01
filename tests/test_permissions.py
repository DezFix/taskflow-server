"""Тесты матрицы прав: кто что может, а кто нет."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.permissions import PERMISSION_KEYS

pytestmark = pytest.mark.asyncio


def auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {users.tokens[role]}"}


async def test_permissions_catalog_is_public(client: AsyncClient) -> None:
    response = await client.get("/api/v1/meta/permissions")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == len(PERMISSION_KEYS)
    assert {item["key"] for item in items} == set(PERMISSION_KEYS)


async def test_head_has_no_system_role_management(client: AsyncClient, users) -> None:
    """Глава отдела управляет отделом, но не может выдать права выше своих."""
    me = await client.get("/api/v1/auth/me", headers=auth(users, "head"))
    permissions = set(me.json()["permissions"])

    assert "users.create" in permissions
    assert "settings.manage_roles" not in permissions


async def test_staff_cannot_create_users(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/users",
        json={"username": "newbie", "full_name": "Новый Сотрудник", "role_ids": []},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "missing_permission"


async def test_staff_cannot_open_admin_audit(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/admin/audit", headers=auth(users, "staff"))
    assert response.status_code == 403


async def test_head_can_read_audit(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/admin/audit", headers=auth(users, "head"))
    assert response.status_code == 200
    assert "items" in response.json()


async def test_staff_cannot_change_own_roles(client: AsyncClient, users) -> None:
    """Даже если знать свой id: смена ролей требует users.edit."""
    response = await client.patch(
        f"/api/v1/users/{users.ids['staff']}",
        json={"full_name": "Новое имя"},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 403


async def test_custom_role_grants_and_removes_access(client: AsyncClient, users) -> None:
    """Матрица прав работает: новая роль даёт ровно то, что в ней отмечено."""
    created = await client.post(
        "/api/v1/roles",
        json={
            "title": "Тестировщик",
            "permissions": [
                "tasks.view",
                "tasks.create",
                "tasks.edit_assigned",
                "chat.direct",
                "positions.view",
                "roles.view",
            ],
        },
        headers=auth(users, "admin"),
    )
    assert created.status_code == 201, created.text
    role_id = created.json()["id"]

    user = await client.post(
        "/api/v1/users",
        json={
            "username": "tester",
            "full_name": "Тестовый Сотрудник",
            "role_ids": [role_id],
        },
        headers=auth(users, "admin"),
    )
    assert user.status_code == 201
    temporary = user.json()["temporary_password"]
    assert temporary

    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "tester", "password": temporary},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Право есть — доступ есть.
    assert (await client.get("/api/v1/tasks", headers=headers)).status_code == 200
    # Права нет — 403 с перечнем недостающих.
    denied = await client.post("/api/v1/positions", json={"title": "Новая"}, headers=headers)
    assert denied.status_code == 403
    assert "positions.create" in denied.json()["error"]["details"]["missing"]


async def test_user_cannot_delete_own_account(client: AsyncClient, users) -> None:
    response = await client.post(
        f"/api/v1/users/{users.ids['head']}/deactivate", headers=auth(users, "head")
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "self_deactivate"


async def test_last_admin_cannot_be_deactivated(client: AsyncClient, users) -> None:
    """Единственного администратора блокировать нельзя — иначе сервер
    останется без управления."""
    response = await client.post(
        f"/api/v1/users/{users.ids['admin']}/deactivate", headers=auth(users, "admin")
    )
    assert response.status_code == 403


async def test_system_role_cannot_be_deleted(client: AsyncClient, users) -> None:
    roles = await client.get("/api/v1/roles", headers=auth(users, "admin"))
    system = next(r for r in roles.json() if r["is_system"])

    response = await client.delete(
        f"/api/v1/roles/{system['id']}", headers=auth(users, "admin")
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "system_role"


async def test_role_in_use_cannot_be_deleted(client: AsyncClient, users) -> None:
    roles = await client.get("/api/v1/roles", headers=auth(users, "admin"))
    staff_role = next(r for r in roles.json() if r["key"] == "Сотрудник")

    # Системную роль удалить нельзя в любом случае, но проверять надо
    # именно запрет на удаление занятой роли — создаём свою.
    custom = await client.post(
        "/api/v1/roles",
        json={"title": "Аналитик", "permissions": ["tasks.view_all"]},
        headers=auth(users, "admin"),
    )
    role_id = custom.json()["id"]
    await client.post(
        "/api/v1/users",
        json={"username": "analyst", "full_name": "Аналитик", "role_ids": [role_id]},
        headers=auth(users, "admin"),
    )

    response = await client.delete(f"/api/v1/roles/{role_id}", headers=auth(users, "admin"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "role_in_use"
    assert staff_role


async def test_unknown_permission_rejected(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/roles",
        json={"title": "Ошибочная", "permissions": ["tasks.do_everything"]},
        headers=auth(users, "admin"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_permission"


async def test_superuser_sees_all_permissions(client: AsyncClient, users) -> None:
    me = await client.get("/api/v1/auth/me", headers=auth(users, "admin"))
    assert set(me.json()["permissions"]) == set(PERMISSION_KEYS)


async def test_system_roles_expose_language_neutral_id(
    client: AsyncClient, users
) -> None:
    """У системных ролей есть английский идентификатор для перевода.

    Ключ роли в базе русский, и менять его нельзя: он участвует в
    проверках прав. Интерфейсу нужен независимый от языка идентификатор.
    """
    from app.permissions import SYSTEM_ROLE_IDS, system_role_id

    response = await client.get("/api/v1/roles", headers=auth(users, "head"))
    assert response.status_code == 200

    by_id = {role["i18n_key"]: role for role in response.json() if role["i18n_key"]}
    assert set(by_id) == set(SYSTEM_ROLE_IDS)
    for identifier, role_key in SYSTEM_ROLE_IDS.items():
        assert by_id[identifier]["key"] == role_key
        assert system_role_id(role_key) == identifier


async def test_custom_role_has_no_i18n_id(client: AsyncClient, users) -> None:
    """Своя роль не переводится: её назвал администратор."""
    response = await client.get("/api/v1/roles", headers=auth(users, "head"))
    custom = [role for role in response.json() if not role["is_system"]]
    for role in custom:
        assert role["i18n_key"] is None
