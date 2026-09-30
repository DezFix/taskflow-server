"""Тесты сотрудников, должностей и ролей."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {users.tokens[role]}"}


async def test_positions_seeded_on_startup(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/positions", headers=auth(users, "staff"))
    assert response.status_code == 200
    titles = {item["title"] for item in response.json()}
    assert "Системный инженер" in titles
    assert "Разработчик" in titles


async def test_create_position(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/positions",
        json={"title": "DevOps", "description": "Серверы и деплой", "sort_order": 5},
        headers=auth(users, "head"),
    )
    assert response.status_code == 201
    assert response.json()["title"] == "DevOps"
    assert response.json()["users_count"] == 0


async def test_duplicate_position_rejected(client: AsyncClient, users) -> None:
    first = await client.post(
        "/api/v1/positions", json={"title": "DevOps"}, headers=auth(users, "head")
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/positions", json={"title": "DevOps"}, headers=auth(users, "head")
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "position_exists"


async def test_staff_cannot_create_position(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/positions", json={"title": "Своя"}, headers=auth(users, "staff")
    )
    assert response.status_code == 403


async def test_position_in_use_cannot_be_deleted(client: AsyncClient, users) -> None:
    position = await client.post(
        "/api/v1/positions", json={"title": "Архитектор"}, headers=auth(users, "head")
    )
    role = await client.get("/api/v1/roles", headers=auth(users, "head"))
    staff_role = next(r for r in role.json() if r["key"] == "Сотрудник")

    await client.patch(
        f"/api/v1/users/{users.ids['staff']}",
        json={"position_id": position.json()["id"]},
        headers=auth(users, "head"),
    )

    response = await client.delete(
        f"/api/v1/positions/{position.json()['id']}", headers=auth(users, "head")
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "position_in_use"
    assert staff_role


async def test_empty_position_can_be_deleted(client: AsyncClient, users) -> None:
    position = await client.post(
        "/api/v1/positions", json={"title": "Временная"}, headers=auth(users, "head")
    )
    response = await client.delete(
        f"/api/v1/positions/{position.json()['id']}", headers=auth(users, "head")
    )
    assert response.status_code == 200


async def test_assign_position_to_user(client: AsyncClient, users) -> None:
    position = await client.post(
        "/api/v1/positions", json={"title": "Аналитик"}, headers=auth(users, "head")
    )
    response = await client.patch(
        f"/api/v1/users/{users.ids['staff']}",
        json={"position_id": position.json()["id"]},
        headers=auth(users, "head"),
    )
    assert response.status_code == 200
    assert response.json()["position"]["title"] == "Аналитик"


async def test_position_can_be_cleared(client: AsyncClient, users) -> None:
    """Должность можно снять: клиент шлёт position_id = null."""
    position = await client.post(
        "/api/v1/positions", json={"title": "Временная"}, headers=auth(users, "head")
    )
    assigned = await client.patch(
        f"/api/v1/users/{users.ids['staff']}",
        json={"position_id": position.json()["id"]},
        headers=auth(users, "head"),
    )
    assert assigned.json()["position"] is not None

    cleared = await client.patch(
        f"/api/v1/users/{users.ids['staff']}",
        json={"position_id": None},
        headers=auth(users, "head"),
    )
    assert cleared.status_code == 200
    assert cleared.json()["position"] is None


async def test_create_user_returns_temporary_password(client: AsyncClient, users) -> None:
    roles = await client.get("/api/v1/roles", headers=auth(users, "head"))
    staff_role = next(r for r in roles.json() if r["key"] == "Сотрудник")

    response = await client.post(
        "/api/v1/users",
        json={
            "username": "newbie",
            "full_name": "Новичок Иванов",
            "role_ids": [staff_role["id"]],
            "phone": "+7 900 000-00-00",
        },
        headers=auth(users, "head"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["temporary_password"]
    assert body["must_change_password"] is True
    assert body["username"] == "newbie"

    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "newbie", "password": body["temporary_password"]},
    )
    assert login.status_code == 200


async def test_duplicate_username_rejected(client: AsyncClient, users) -> None:
    roles = await client.get("/api/v1/roles", headers=auth(users, "head"))
    staff_role = next(r for r in roles.json() if r["key"] == "Сотрудник")
    response = await client.post(
        "/api/v1/users",
        json={"username": "staff", "full_name": "Клон", "role_ids": [staff_role["id"]]},
        headers=auth(users, "head"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "username_taken"


async def test_user_requires_at_least_one_role(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/users",
        json={"username": "norole", "full_name": "Без роли", "role_ids": []},
        headers=auth(users, "head"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "role_required"


async def test_reset_password_revokes_sessions(client: AsyncClient, users) -> None:
    before = await client.get("/api/v1/auth/sessions", headers=auth(users, "staff"))
    assert len(before.json()) >= 1

    response = await client.post(
        f"/api/v1/users/{users.ids['staff']}/reset-password",
        json={"must_change_password": True},
        headers=auth(users, "head"),
    )
    assert response.status_code == 200
    new_password = response.json()["temporary_password"]
    assert new_password

    # Старый пароль больше не подходит.
    login = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": "StaffPass123"}
    )
    assert login.status_code == 401

    fresh = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": new_password}
    )
    assert fresh.status_code == 200


async def test_deactivated_user_disappears_from_active_list(
    client: AsyncClient, users
) -> None:
    await client.post(
        f"/api/v1/users/{users.ids['staff2']}/deactivate", headers=auth(users, "head")
    )
    response = await client.get("/api/v1/users?is_active=true", headers=auth(users, "head"))
    assert all(item["username"] != "staff2" for item in response.json())

    inactive = await client.get("/api/v1/users?is_active=false", headers=auth(users, "head"))
    assert [item["username"] for item in inactive.json()] == ["staff2"]


async def test_reactivate_user(client: AsyncClient, users) -> None:
    await client.post(
        f"/api/v1/users/{users.ids['staff2']}/deactivate", headers=auth(users, "head")
    )
    response = await client.post(
        f"/api/v1/users/{users.ids['staff2']}/activate", headers=auth(users, "head")
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is True

    login = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff2", "password": "StaffPass123"}
    )
    assert login.status_code == 200


async def test_user_search(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/users?search=staff", headers=auth(users, "head"))
    assert response.status_code == 200
    usernames = {item["username"] for item in response.json()}
    assert {"staff", "staff2"} <= usernames


async def test_department_stats(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/users/stats/overview", headers=auth(users, "head"))
    assert response.status_code == 200
    assert response.json()["active"] == 4
    assert response.json()["inactive"] == 0


async def test_update_own_profile(client: AsyncClient, users) -> None:
    response = await client.patch(
        "/api/v1/auth/me",
        json={"full_name": "Обновлённое Имя", "phone": "+7 999 111-22-33"},
        headers=auth(users, "staff"),
    )
    assert response.status_code == 200
    assert response.json()["full_name"] == "Обновлённое Имя"
    assert response.json()["phone"] == "+7 999 111-22-33"


async def test_role_permissions_are_sorted_and_deduped(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/roles",
        json={
            "title": "Дубли",
            "permissions": ["tasks.view", "tasks.view", "chat.direct"],
        },
        headers=auth(users, "admin"),
    )
    assert response.status_code == 201
    assert response.json()["permissions"] == ["chat.direct", "tasks.view"]


async def test_permission_matrix_endpoint(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/roles/permissions/matrix", headers=auth(users, "head"))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] > 0
    assert len(body["groups"]) >= 5
    keys = {role["key"] for role in body["roles"]}
    assert "Сотрудник" in keys
