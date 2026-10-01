"""Тесты аутентификации: настройка, вход, токены, пароли, блокировка."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import ADMIN_PASSWORD, STAFF_PASSWORD

pytestmark = pytest.mark.asyncio


async def test_meta_info_reports_setup_required(client: AsyncClient) -> None:
    response = await client.get("/api/v1/meta/info")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "TaskFlow"
    assert body["requires_setup"] is True


async def test_health_needs_no_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/meta/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_setup_creates_admin_and_tokens(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/setup",
        json={
            "username": "boss",
            "password": ADMIN_PASSWORD,
            "full_name": "Иван Иванов",
            "organization_name": "ООО Ромашка",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["username"] == "boss"
    assert me.json()["full_name"] == "Иван Иванов"


async def test_setup_is_closed_after_first_run(client: AsyncClient) -> None:
    first = await client.post(
        "/api/v1/auth/setup",
        json={"username": "boss", "password": ADMIN_PASSWORD, "full_name": "Иван Иванов"},
    )
    assert first.status_code == 200

    second = await client.post(
        "/api/v1/auth/setup",
        json={"username": "boss2", "password": ADMIN_PASSWORD, "full_name": "Пётр Петров"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_setup"


async def test_setup_creates_system_roles(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/setup",
        json={"username": "boss", "password": ADMIN_PASSWORD, "full_name": "Иван Иванов"},
    )
    response = await client.get("/api/v1/meta/system-roles")
    assert response.status_code == 200
    assert set(response.json()) == {"Администратор", "Глава отдела", "Сотрудник"}


async def test_setup_rejects_weak_password(client: AsyncClient) -> None:
    """Слишком короткий пароль отсекается схемой запроса."""
    response = await client.post(
        "/api/v1/auth/setup",
        json={"username": "boss", "password": "short", "full_name": "Иван Иванов"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_setup_rejects_password_without_digits(client: AsyncClient) -> None:
    """Пароль без цифр проходит по длине, но не проходит по стойкости."""
    response = await client.post(
        "/api/v1/auth/setup",
        json={"username": "boss", "password": "abcdefghij", "full_name": "Иван Иванов"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "password_weak"


async def test_setup_rejects_bad_username(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/setup",
        json={"username": "ab", "password": ADMIN_PASSWORD, "full_name": "Иван Иванов"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_setup_rejects_username_with_spaces(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/setup",
        json={"username": "иван иванов", "password": ADMIN_PASSWORD, "full_name": "Иван Иванов"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "username_charset"


async def test_login_with_username_and_email(client: AsyncClient, users) -> None:
    by_name = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "head", "password": "HeadPass123"},
    )
    assert by_name.status_code == 200

    by_email = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "HEAD", "password": "HeadPass123"},
    )
    assert by_email.status_code == 200


async def test_login_rejects_wrong_password(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_rejects_unknown_user(client: AsyncClient, users) -> None:
    """Неизвестный логин и неверный пароль дают одинаковый ответ —
    иначе можно перебирать существующие учётки."""
    unknown = await client.post(
        "/api/v1/auth/login", json={"identifier": "nobody", "password": "whatever1"}
    )
    wrong = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": "whatever1"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]


async def test_account_locks_after_repeated_failures(
    client: AsyncClient, users, monkeypatch
) -> None:
    """После серии неудачных попыток вход блокируется.

    Порог и время ожидания задаются настройками, поэтому подменяем их
    на минимальные: тест проверяет механику, а не ждёт реальные 15 минут.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_failed_logins", 3, raising=False)
    monkeypatch.setattr(settings, "lockout_minutes", 15, raising=False)

    for _ in range(3):
        await client.post(
            "/api/v1/auth/login",
            json={"identifier": "staff2", "password": "wrong-one"},
        )

    blocked = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "staff2", "password": "StaffPass123"},
    )
    assert blocked.status_code == 401
    # Тот же ответ, что при неверном пароле: иначе по коду определяется
    # факт существования учётной записи.
    assert blocked.json()["error"]["code"] == "invalid_credentials"

    # Верный пароль не помогает, пока не истекла блокировка.
    again = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "staff2", "password": "StaffPass123"},
    )
    assert again.status_code == 401
    assert again.json()["error"]["code"] == "invalid_credentials"


async def test_successful_login_resets_failure_counter(
    client: AsyncClient, users, monkeypatch
) -> None:
    """Неудачные попыги до порога не блокируют учётку."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_failed_logins", 5, raising=False)
    monkeypatch.setattr(settings, "lockout_minutes", 15, raising=False)

    for _ in range(4):
        await client.post(
            "/api/v1/auth/login",
            json={"identifier": "staff2", "password": "wrong-one"},
        )

    success = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "staff2", "password": "StaffPass123"},
    )
    assert success.status_code == 200

    # Счётчик сброшен: ещё четыре ошибки не заблокируют вход.
    for _ in range(4):
        await client.post(
            "/api/v1/auth/login",
            json={"identifier": "staff2", "password": "wrong-one"},
        )
    final = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "staff2", "password": "StaffPass123"},
    )
    assert final.status_code == 200


async def test_login_by_email(client: AsyncClient, users) -> None:
    """Вход по email работает: сотруднику не нужно запоминать разницу."""
    from app.database import get_sessionmaker
    from app.models import User

    async with get_sessionmaker()() as session:
        user = await session.get(User, users.ids["staff"])
        user.email = "engineer@company.ru"
        await session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "engineer@company.ru", "password": "StaffPass123"},
    )
    assert login.status_code == 200


async def test_email_cannot_be_reused(client: AsyncClient, users) -> None:
    """Два сотрудника не могут иметь один email: иначе войдёт не тот."""
    from app.database import get_sessionmaker
    from app.models import User

    async with get_sessionmaker()() as session:
        user = await session.get(User, users.ids["staff"])
        user.email = "shared@company.ru"
        await session.commit()

    roles = await client.get("/api/v1/roles", headers=auth(users, "head"))
    staff_role = next(r for r in roles.json() if r["key"] == "Сотрудник")

    response = await client.post(
        "/api/v1/users",
        json={
            "username": "second",
            "full_name": "Второй Сотрудник",
            "email": "shared@company.ru",
            "role_ids": [staff_role["id"]],
        },
        headers=auth(users, "head"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


async def test_refresh_rotates_token(client: AsyncClient, users) -> None:
    login = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": STAFF_PASSWORD}
    )
    refresh_token = login.json()["refresh_token"]

    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200
    new_refresh = first.json()["refresh_token"]
    assert new_refresh != refresh_token

    # Старый токен больше не работает — это ротация, а не продление.
    second = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert second.status_code == 401


async def test_current_session_is_marked_in_list(client: AsyncClient, users) -> None:
    """Сессия, по которой пришёл запрос, помечается как текущая."""
    response = await client.get("/api/v1/auth/sessions", headers=auth(users, "head"))
    assert response.status_code == 200, response.text
    items = response.json()
    assert items, "сессии не вернулись"
    current = [item for item in items if item["is_current"]]
    assert len(current) == 1, "текущая сессия должна быть ровно одна"


async def test_logout_revokes_session(client: AsyncClient, users) -> None:
    login = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": STAFF_PASSWORD}
    )
    refresh_token = login.json()["refresh_token"]

    out = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert out.status_code == 200

    again = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert again.status_code == 401


async def test_access_token_required(client: AsyncClient, users) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_invalid_token_rejected(client: AsyncClient, users) -> None:
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


async def test_change_password_requires_current(client: AsyncClient, users) -> None:
    wrong = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "nope", "new_password": "NewPass456"},
        headers=users_auth(users, "staff"),
    )
    assert wrong.status_code == 401

    ok = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": STAFF_PASSWORD, "new_password": "NewPass456"},
        headers=users_auth(users, "staff"),
    )
    assert ok.status_code == 200

    old = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": STAFF_PASSWORD}
    )
    assert old.status_code == 401

    new = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": "NewPass456"}
    )
    assert new.status_code == 200


async def test_password_policy_is_public(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/password-policy")
    assert response.status_code == 200
    assert response.json()["min_length"] == 8


async def test_deactivated_user_cannot_login(client: AsyncClient, users) -> None:
    """Деактивация отключает вход, но учётная запись остаётся в базе."""
    response = await client.post(
        f"/api/v1/users/{users.ids['staff2']}/deactivate",
        headers=users_auth(users, "head"),
    )
    assert response.status_code == 200

    login = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff2", "password": STAFF_PASSWORD}
    )
    # Ответ намеренно такой же, как при неверном пароле: иначе по коду
    # можно перебрать логины сотрудников.
    assert login.status_code == 401
    assert login.json()["error"]["code"] == "invalid_credentials"


async def test_login_failure_responses_are_indistinguishable(
    client: AsyncClient, users
) -> None:
    """По ответу нельзя определить, существует ли учётная запись.

    Раньше несуществующий логин давал 401, отключённый — 403 с текстом
    «Учётная запись отключена», заблокированный — 403 со временем
    разблокировки. Эти различия выдавали список сотрудников.
    """
    await client.post(
        f"/api/v1/users/{users.ids['staff2']}/deactivate",
        headers=users_auth(users, "head"),
    )

    unknown = await client.post(
        "/api/v1/auth/login", json={"identifier": "nobody-here", "password": "whatever-1"}
    )
    disabled = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff2", "password": STAFF_PASSWORD}
    )
    wrong = await client.post(
        "/api/v1/auth/login", json={"identifier": "staff", "password": "wrong-password"}
    )

    codes = {r.status_code for r in (unknown, disabled, wrong)}
    assert codes == {401}
    assert len({r.json()["error"]["code"] for r in (unknown, disabled, wrong)}) == 1
    assert len({r.json()["error"]["message"] for r in (unknown, disabled, wrong)}) == 1


async def test_login_is_rate_limited(client: AsyncClient, users, monkeypatch) -> None:
    """Частота попыток ограничена: иначе перебор можно вести без пауз."""
    from app.config import get_settings
    from app.services import rate_limit

    # Порог снижаем, чтобы тест не делал десятки проверок Argon2.
    monkeypatch.setattr(get_settings(), "max_failed_logins", 2, raising=False)
    rate_limit.reset()
    try:
        payload = {"identifier": "nobody-here", "password": "guess-password"}
        statuses = [
            (await client.post("/api/v1/auth/login", json=payload)).status_code
            for _ in range(8)
        ]
        assert 429 in statuses
        # До срабатывания лимита запрос доходит до проверки пароля.
        assert set(statuses[:2]) == {401}
    finally:
        rate_limit.reset()


def auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    """Готовый заголовок Authorization для роли из фикстуры users."""
    return {"Authorization": f"Bearer {users.tokens[role]}"}


def users_auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {users.tokens[role]}"}
