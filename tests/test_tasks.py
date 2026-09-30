"""Тесты задач: создание, права, статусы, история, комментарии, отчёты."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def auth(users, role: str) -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {users.tokens[role]}"}


async def test_head_creates_task_for_staff(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/tasks",
        json={
            "title": "Заменить жёсткий диск",
            "description": "Сотрудник жалуется на ошибки чтения",
            "assignee_id": users.ids["staff"],
            "priority": "high",
            "tags": ["Оборудование", "срочное"],
        },
        headers=auth(users, "head"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] == "Заменить жёсткий диск"
    assert body["status"] == "new"
    assert body["priority"] == "high"
    assert body["assignee"]["id"] == users.ids["staff"]
    assert {t["name"] for t in body["tags"]} == {"Оборудование", "срочное"}


async def test_task_sequence_is_monotonic(client: AsyncClient, users) -> None:
    first = await client.post(
        "/api/v1/tasks", json={"title": "Первая задача"}, headers=auth(users, "head")
    )
    second = await client.post(
        "/api/v1/tasks", json={"title": "Вторая задача"}, headers=auth(users, "head")
    )
    assert first.json()["id"] != second.json()["id"]


async def test_duplicate_tags_are_merged(client: AsyncClient, users) -> None:
    response = await client.post(
        "/api/v1/tasks",
        json={"title": "Задача с метками", "tags": ["Сеть", "сеть", "СЕТЬ"]},
        headers=auth(users, "head"),
    )
    assert response.status_code == 201
    assert len(response.json()["tags"]) == 1


async def test_task_list_is_paginated(client: AsyncClient, users) -> None:
    for index in range(7):
        await client.post(
            "/api/v1/tasks",
            json={"title": f"Задача {index}", "assignee_id": users.ids["staff"]},
            headers=auth(users, "head"),
        )

    first_page = await client.get(
        "/api/v1/tasks?limit=3", headers=auth(users, "head")
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert len(body["items"]) == 3
    assert body["has_more"] is True
    assert body["total"] == 7

    second_page = await client.get(
        f"/api/v1/tasks?limit=3&cursor={body['next_cursor']}", headers=auth(users, "head")
    )
    second_ids = {item["id"] for item in second_page.json()["items"]}
    first_ids = {item["id"] for item in body["items"]}
    assert not (first_ids & second_ids)


async def test_staff_sees_only_own_tasks(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/tasks",
        json={"title": "Задача для сотрудника", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    await client.post(
        "/api/v1/tasks",
        json={"title": "Чужая задача", "assignee_id": users.ids["staff2"]},
        headers=auth(users, "head"),
    )

    mine = await client.get("/api/v1/tasks", headers=auth(users, "staff"))
    titles = {item["title"] for item in mine.json()["items"]}
    assert titles == {"Задача для сотрудника"}


async def test_staff_cannot_open_foreign_task(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks",
        json={"title": "Чужая задача", "assignee_id": users.ids["staff2"]},
        headers=auth(users, "head"),
    )
    response = await client.get(
        f"/api/v1/tasks/{created.json()['id']}", headers=auth(users, "staff")
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "task_access_denied"


async def test_search_filters_tasks(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/tasks", json={"title": "Настроить принтер"}, headers=auth(users, "head")
    )
    await client.post(
        "/api/v1/tasks", json={"title": "Обновить Windows"}, headers=auth(users, "head")
    )

    found = await client.get(
        "/api/v1/tasks?search=принтер", headers=auth(users, "head")
    )
    titles = {item["title"] for item in found.json()["items"]}
    assert titles == {"Настроить принтер"}


async def test_status_change_is_recorded_in_history(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks",
        json={"title": "Починить сервер", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    task_id = created.json()["id"]

    changed = await client.post(
        f"/api/v1/tasks/{task_id}/status",
        json={"status": "done", "comment": "Сервер работает"},
        headers=auth(users, "head"),
    )
    assert changed.status_code == 200
    assert changed.json()["status"] == "done"
    assert changed.json()["completed_at"] is not None

    history = await client.get(
        f"/api/v1/tasks/{task_id}/history", headers=auth(users, "head")
    )
    fields = {entry["field"] for entry in history.json()}
    assert "status" in fields

    comments = await client.get(
        f"/api/v1/tasks/{task_id}/comments", headers=auth(users, "head")
    )
    assert any("работает" in (c["body"] or "") for c in comments.json())


async def test_reopening_task_clears_completion(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks", json={"title": "Задача"}, headers=auth(users, "head")
    )
    task_id = created.json()["id"]

    await client.post(
        f"/api/v1/tasks/{task_id}/status", json={"status": "done"}, headers=auth(users, "head")
    )
    reopened = await client.post(
        f"/api/v1/tasks/{task_id}/status",
        json={"status": "in_progress"},
        headers=auth(users, "head"),
    )
    assert reopened.json()["status"] == "in_progress"
    assert reopened.json()["completed_at"] is None


async def test_assign_task(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks", json={"title": "Без исполнителя"}, headers=auth(users, "head")
    )
    task_id = created.json()["id"]

    assigned = await client.post(
        f"/api/v1/tasks/{task_id}/assign",
        json={"assignee_id": users.ids["staff2"], "comment": "Передано отделу"},
        headers=auth(users, "head"),
    )
    assert assigned.status_code == 200
    assert assigned.json()["assignee"]["id"] == users.ids["staff2"]


async def test_assign_rejects_inactive_user(client: AsyncClient, users) -> None:
    await client.post(
        f"/api/v1/users/{users.ids['staff2']}/deactivate", headers=auth(users, "head")
    )
    created = await client.post(
        "/api/v1/tasks", json={"title": "Задача"}, headers=auth(users, "head")
    )
    response = await client.post(
        f"/api/v1/tasks/{created.json()['id']}/assign",
        json={"assignee_id": users.ids["staff2"]},
        headers=auth(users, "head"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "assignee_invalid"


async def test_work_report_moves_task_to_review(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks",
        json={"title": "Установить ПО", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    task_id = created.json()["id"]

    report = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"body": "Установил, проверил", "is_work_report": True},
        headers=auth(users, "staff"),
    )
    assert report.status_code == 201
    assert report.json()["is_work_report"] is True

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(users, "head"))
    assert detail.json()["status"] == "review"


async def test_empty_comment_rejected(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks", json={"title": "Задача"}, headers=auth(users, "head")
    )
    response = await client.post(
        f"/api/v1/tasks/{created.json()['id']}/comments",
        json={"body": "   "},
        headers=auth(users, "head"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "comment_empty"


async def test_overdue_flag(client: AsyncClient, users) -> None:
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    created = await client.post(
        "/api/v1/tasks",
        json={"title": "Просроченная", "due_at": past, "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    assert created.json()["is_overdue"] is True

    filter_overdue = await client.get("/api/v1/tasks?overdue=true", headers=auth(users, "head"))
    assert len(filter_overdue.json()["items"]) == 1


async def test_overdue_filter_excludes_finished_tasks(
    client: AsyncClient, users
) -> None:
    """Выполненная и отменённая задача с прошедшим сроком не просрочена."""
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    done = await client.post(
        "/api/v1/tasks",
        json={"title": "Сделано в срок", "due_at": past, "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    await client.post(
        f"/api/v1/tasks/{done.json()['id']}/status",
        json={"status": "done"},
        headers=auth(users, "head"),
    )

    cancelled = await client.post(
        "/api/v1/tasks",
        json={"title": "Отменено", "due_at": past, "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    await client.post(
        f"/api/v1/tasks/{cancelled.json()['id']}/status",
        json={"status": "cancelled"},
        headers=auth(users, "head"),
    )

    await client.post(
        "/api/v1/tasks",
        json={"title": "Ещё в работе", "due_at": past, "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )

    overdue = await client.get("/api/v1/tasks?overdue=true", headers=auth(users, "head"))
    titles = {item["title"] for item in overdue.json()["items"]}
    assert titles == {"Ещё в работе"}

    # У выполненной задачи срок в прошлом, но флаг не горит.
    all_tasks = await client.get("/api/v1/tasks", headers=auth(users, "head"))
    finished = next(
        item for item in all_tasks.json()["items"] if item["title"] == "Сделано в срок"
    )
    assert finished["is_overdue"] is False


async def test_filter_by_status_and_priority(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/tasks",
        json={"title": "Срочная", "priority": "urgent", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    await client.post(
        "/api/v1/tasks",
        json={"title": "Обычная", "priority": "low", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )

    urgent = await client.get("/api/v1/tasks?priority=urgent", headers=auth(users, "head"))
    assert {i["title"] for i in urgent.json()["items"]} == {"Срочная"}

    in_progress = await client.post(
        "/api/v1/tasks", json={"title": "В работе"}, headers=auth(users, "head")
    )
    await client.post(
        f"/api/v1/tasks/{in_progress.json()['id']}/status",
        json={"status": "in_progress"},
        headers=auth(users, "head"),
    )
    by_status = await client.get("/api/v1/tasks?status=in_progress", headers=auth(users, "head"))
    assert {i["title"] for i in by_status.json()["items"]} == {"В работе"}


async def test_delete_requires_cancel_first(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks", json={"title": "Активная задача"}, headers=auth(users, "head")
    )
    task_id = created.json()["id"]

    refused = await client.delete(f"/api/v1/tasks/{task_id}", headers=auth(users, "head"))
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "cancel_first"

    await client.post(
        f"/api/v1/tasks/{task_id}/status", json={"status": "cancelled"}, headers=auth(users, "head")
    )
    deleted = await client.delete(f"/api/v1/tasks/{task_id}", headers=auth(users, "head"))
    assert deleted.status_code == 200

    listed = await client.get("/api/v1/tasks", headers=auth(users, "head"))
    assert all(i["id"] != task_id for i in listed.json()["items"])


async def test_staff_cannot_delete_task(client: AsyncClient, users) -> None:
    created = await client.post(
        "/api/v1/tasks", json={"title": "Задача"}, headers=auth(users, "head")
    )
    response = await client.delete(
        f"/api/v1/tasks/{created.json()['id']}", headers=auth(users, "staff")
    )
    assert response.status_code == 403


async def test_task_summary_report(client: AsyncClient, users) -> None:
    done = await client.post(
        "/api/v1/tasks",
        json={"title": "Готово", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    await client.post(
        f"/api/v1/tasks/{done.json()['id']}/status",
        json={"status": "done"},
        headers=auth(users, "head"),
    )
    await client.post(
        "/api/v1/tasks",
        json={"title": "В работе", "assignee_id": users.ids["staff"], "status": "in_progress"},
        headers=auth(users, "head"),
    )

    summary = await client.get("/api/v1/tasks/reports/summary", headers=auth(users, "head"))
    assert summary.status_code == 200
    body = summary.json()
    assert body["total"] == 2
    assert body["by_status"]["done"] == 1
    assert body["by_status"]["in_progress"] == 1
    assert body["completion_rate"] == 100.0


async def test_user_load_report(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/tasks",
        json={"title": "Задача 1", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    await client.post(
        "/api/v1/tasks",
        json={"title": "Задача 2", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )

    report = await client.get("/api/v1/tasks/reports/user-load", headers=auth(users, "head"))
    assert report.status_code == 200
    rows = {row["user"]["id"]: row for row in report.json()["rows"]}
    assert rows[users.ids["staff"]]["assigned"] == 2
    assert rows[users.ids["staff2"]]["assigned"] == 0


async def test_csv_export(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/tasks", json={"title": "Экспорт"}, headers=auth(users, "head")
    )
    response = await client.get(
        "/api/v1/admin/reports/tasks.csv", headers=auth(users, "head")
    )
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    text = response.content.decode("utf-8-sig")
    assert "Заголовок" in text
    assert "Экспорт" in text


async def test_csv_export_of_staff_shows_only_own_tasks(client: AsyncClient, users) -> None:
    """Сотрудник выгружает только свои задачи: фильтр по себе."""
    mine = await client.post(
        "/api/v1/tasks",
        json={"title": "Моя задача", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    await client.post(
        "/api/v1/tasks",
        json={"title": "Чужая задача", "assignee_id": users.ids["head"]},
        headers=auth(users, "head"),
    )
    assert mine.status_code == 201

    response = await client.get(
        "/api/v1/admin/reports/tasks.csv", headers=auth(users, "staff")
    )
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    assert "Моя задача" in text
    assert "Чужая задача" not in text


async def test_my_stats(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/tasks",
        json={"title": "Моя задача", "assignee_id": users.ids["staff"]},
        headers=auth(users, "head"),
    )
    stats = await client.get("/api/v1/tasks/stats/my", headers=auth(users, "staff"))
    assert stats.status_code == 200
    assert stats.json()["total"] == 1


async def test_tags_endpoint(client: AsyncClient, users) -> None:
    await client.post(
        "/api/v1/tasks", json={"title": "С меткой", "tags": ["Авария"]}, headers=auth(users, "head")
    )
    response = await client.get("/api/v1/tasks/tags/list", headers=auth(users, "head"))
    assert response.status_code == 200
    assert "Авария" in {item["name"] for item in response.json()}
