"""Живая проверка сервера: полный сценарий сотрудника от настройки до отчёта.

Скрипт поднимает uvicorn на случайном порту, проходит весь путь через
HTTP API и печатает результат. Используется как ручной smoke-тест
после изменений:

    python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="taskflow-smoke-"))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(TMP / 'smoke.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(TMP / "storage"))
os.environ.setdefault("MODELS_DIR", str(TMP / "models"))
os.environ.setdefault("BACKUP_DIR", str(TMP / "backups"))
os.environ.setdefault("JWT_SECRET", "smoke-secret-key-for-local-testing-only-0123456789")
os.environ.setdefault("VOICE_ENABLED", "false")
os.environ.setdefault("WEB_ROOT", "")

import httpx  # noqa: E402

# Ответы HTTP печатаются нашёлкированной библиотекой и забивают вывод.
logging.getLogger("httpx").setLevel(logging.WARNING)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} — {detail}")
        print(f"  [FAIL] {name} {detail}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

WAV = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x40\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


async def run() -> int:
    import uvicorn

    from app.main import app

    port = free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)

    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=30, event_hooks={"request": []}
        ) as client:
            await scenario(client, base)
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

    print()
    print(f"Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}")
    if FAILED:
        print("\nПроблемы:")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


async def scenario(client: httpx.AsyncClient, base: str) -> None:
    print("\n1. Проверка сервера")
    health = await client.get("/api/v1/meta/health")
    check("Health-check отвечает", health.status_code == 200, health.text)

    info = await client.get("/api/v1/meta/info")
    body = info.json()
    check("Сервер сообщает версию", bool(body.get("version")))
    check("Требуется первоначальная настройка", body.get("requires_setup") is True)

    docs = await client.get("/openapi.json")
    schema = docs.json()
    endpoint_count = sum(len(v) for v in schema["paths"].values())
    check(f"OpenAPI содержит эндпоинты ({endpoint_count})", endpoint_count > 80)

    print("\n2. Первоначальная настройка")
    setup = await client.post(
        "/api/v1/auth/setup",
        json={
            "username": "director",
            "password": "DirectorPass123",
            "full_name": "Директор Иванов",
            "organization_name": "ООО Проверка",
        },
    )
    check("Администратор создан", setup.status_code == 200, setup.text)
    admin = {"Authorization": f"Bearer {setup.json()['access_token']}"}

    me = await client.get("/api/v1/auth/me", headers=admin)
    check("Профиль администратора получен", me.status_code == 200)

    catalog = await client.get("/api/v1/meta/permissions")
    catalog_keys = {item["key"] for item in catalog.json()}
    granted = set(me.json()["permissions"])
    check(
        f"У администратора все права ({len(catalog_keys)})",
        granted == catalog_keys,
        f"не хватает: {sorted(catalog_keys - granted)}",
    )

    roles = await client.get("/api/v1/roles", headers=admin)
    role_keys = {r["key"] for r in roles.json()}
    check(
        "Созданы системные роли",
        {"Администратор", "Глава отдела", "Сотрудник"} <= role_keys,
        str(role_keys),
    )
    role_map = {r["key"]: r["id"] for r in roles.json()}
    check("Базовые роли созданы", len(roles.json()) >= 3)

    print("\n3. Сотрудники")
    gone = await client.get("/api/v1/positions", headers=admin)
    check("Эндпоинт должностей удалён", gone.status_code == 404, gone.text)

    created_users: dict[str, str] = {}
    temporary: dict[str, str] = {}
    for login, name, role_key in (
        ("manager", "Глава Отдела Петров", "Глава отдела"),
        ("engineer", "Инженер Сидоров", "Сотрудник"),
        ("engineer2", "Инженер Кузнецов", "Сотрудник"),
    ):
        response = await client.post(
            "/api/v1/users",
            json={
                "username": login,
                "full_name": name,
                "role_ids": [role_map[role_key]],
            },
            headers=admin,
        )
        check(f"Сотрудник {login} создан", response.status_code == 201, response.text)
        created_users[login] = response.json()["id"]
        temporary[login] = response.json()["temporary_password"]

    print("\n4. Вход сотрудников")
    tokens: dict[str, str] = {}
    for login in created_users:
        response = await client.post(
            "/api/v1/auth/login",
            json={"identifier": login, "password": temporary[login]},
        )
        check(f"Вход {login}", response.status_code == 200, response.text)
        tokens[login] = response.json()["access_token"]

    manager = {"Authorization": f"Bearer {tokens['manager']}"}
    engineer = {"Authorization": f"Bearer {tokens['engineer']}"}
    engineer2 = {"Authorization": f"Bearer {tokens['engineer2']}"}

    me_manager = await client.get("/api/v1/auth/me", headers=manager)
    check(
        "У главы отдела есть управленческие права",
        "users.create" in me_manager.json()["permissions"],
    )
    check(
        "У главы отдела нет системных прав",
        "settings.manage_roles" not in me_manager.json()["permissions"],
    )

    denied = await client.get("/api/v1/admin/audit", headers=engineer)
    check("Сотруднику закрыт журнал действий", denied.status_code == 403, denied.text)

    print("\n5. Задачи")
    task = await client.post(
        "/api/v1/tasks",
        json={
            "title": "Заменить жёсткий диск в ПК бухгалтера",
            "description": "Система зависает, SMART показывает ошибки",
            "assignee_id": created_users["engineer"],
            "priority": "urgent",
            "due_at": "2026-12-31T18:00:00Z",
            "tags": ["Оборудование", "Авария"],
        },
        headers=manager,
    )
    check("Задача создана", task.status_code == 201, task.text)
    task_id = task.json()["id"]
    check("Теги прикрепились", len(task.json()["tags"]) == 2)

    mine = await client.get("/api/v1/tasks", headers=engineer)
    check("Сотрудник видит свою задачу", len(mine.json()["items"]) == 1)

    foreign = await client.get(f"/api/v1/tasks/{task_id}", headers=engineer2)
    check("Сотрудник не видит чужую задачу", foreign.status_code == 403, foreign.text)

    moved = await client.post(
        f"/api/v1/tasks/{task_id}/status",
        json={"status": "in_progress"},
        headers=engineer,
    )
    check("Сотрудник взял задачу в работу", moved.status_code == 200, moved.text)

    print("\n6. Фотоотчёт")
    upload = await client.post(
        "/api/v1/files",
        files={"file": ("report.png", PNG, "image/png")},
        data={"task_id": task_id},
        headers=engineer,
    )
    check("Файл загружен", upload.status_code == 201, upload.text)
    file_id = upload.json()["id"]

    report = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={
            "body": "Заменил диск, система работает стабильно",
            "attachment_ids": [file_id],
            "is_work_report": True,
        },
        headers=engineer,
    )
    check("Отчёт добавлен", report.status_code == 201, report.text)
    check("Отчёт приложил файл", len(report.json()["attachments"]) == 1)

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=manager)
    check(
        "Отчёт перевёл задачу на проверку",
        detail.json()["status"] == "review",
        detail.json()["status"],
    )

    history = await client.get(f"/api/v1/tasks/{task_id}/history", headers=manager)
    fields = {entry["field"] for entry in history.json()}
    check("История изменений заполнена", "status" in fields, str(fields))

    accepted = await client.post(
        f"/api/v1/tasks/{task_id}/status",
        json={"status": "done", "comment": "Принято"},
        headers=manager,
    )
    check("Глава принял работу", accepted.json()["status"] == "done")

    blocked = await client.post(
        "/api/v1/files",
        files={"file": ("virus.png", b"MZ\x90\x00" + b"\x00" * 64, "image/png")},
        headers=engineer,
    )
    check("Исполняемый файл отклонён", blocked.status_code == 400, blocked.text)

    print("\n7. Чат и голосовые")
    chat = await client.post(
        "/api/v1/chats/direct",
        json={"user_id": created_users["engineer2"]},
        headers=engineer,
    )
    check("Диалог открыт", chat.status_code == 200, chat.text)
    chat_id = chat.json()["id"]

    again = await client.post(
        "/api/v1/chats/direct",
        json={"user_id": created_users["engineer2"]},
        headers=engineer,
    )
    check("Повторный диалог не создал дубль", again.json()["id"] == chat_id)

    message = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"body": "Привет, нужна помощь с принтером"},
        headers=engineer,
    )
    check("Сообщение отправлено", message.status_code == 201, message.text)

    unread = await client.get("/api/v1/chats/unread/total", headers=engineer2)
    check("Счётчик непрочитанных работает", unread.json()["unread"] == 1, unread.text)

    voice = await client.post(
        f"/api/v1/chats/{chat_id}/voice",
        files={"file": ("voice.wav", WAV, "audio/wav")},
        data={"duration_sec": "3.5"},
        headers=engineer,
    )
    check("Голосовое принято", voice.status_code == 201, voice.text)
    check("Голосовое помечено как voice", voice.json()["kind"] == "voice")
    check(
        "Создана запись расшифровки",
        voice.json().get("transcript") is not None,
    )

    capabilities = await client.get("/api/v1/chats/voice/capabilities", headers=engineer)
    check("Форматы аудио отдаются клиенту", "m4a" in capabilities.json()["formats"])

    group = await client.post(
        "/api/v1/chats/groups",
        json={"title": "Отдел ИТ", "member_ids": list(created_users.values())},
        headers=manager,
    )
    check("Группа создана", group.status_code == 201, group.text)

    print("\n8. Отчёты и администрирование")
    summary = await client.get("/api/v1/tasks/reports/summary", headers=manager)
    check("Сводка по задачам", summary.json()["total"] == 1, summary.text)
    check("Процент выполнения посчитан", summary.json()["completion_rate"] == 100.0)

    load = await client.get("/api/v1/tasks/reports/user-load", headers=manager)
    check(
        "Нагрузка по сотрудникам",
        any(row["user"]["username"] == "engineer" for row in load.json()["rows"]),
    )

    csv = await client.get("/api/v1/admin/reports/tasks.csv", headers=manager)
    check("Выгрузка CSV", csv.status_code == 200 and "Заменить" in csv.text)

    audit = await client.get("/api/v1/admin/audit", headers=manager)
    check("Журнал действий заполнен", audit.json()["total"] > 3, str(audit.json()["total"]))

    backup = await client.post("/api/v1/admin/backup", headers=admin)
    check("Резервная копия создана", backup.status_code == 200, backup.text)
    check("В копии есть база и файлы", backup.json()["includes_database"] and backup.json()["files"] >= 1)

    info_sys = await client.get("/api/v1/admin/system/info", headers=admin)
    check("Сведения о системе", info_sys.json()["database"] == "sqlite", info_sys.text)

    print("\n9. Сброс пароля и блокировка")
    reset = await client.post(
        f"/api/v1/users/{created_users['engineer2']}/reset-password",
        json={},
        headers=manager,
    )
    check("Пароль сброшен", reset.status_code == 200 and reset.json()["temporary_password"])

    old_login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "engineer2", "password": temporary["engineer2"]},
    )
    check("Старый пароль перестал работать", old_login.status_code == 401)

    blocked_user = await client.post(
        f"/api/v1/users/{created_users['engineer']}/deactivate", headers=manager
    )
    check("Сотрудник деактивирован", blocked_user.status_code == 200)
    blocked_login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "engineer", "password": temporary["engineer"]},
    )
    check("Деактивированный не входит", blocked_login.status_code == 403)

    print("\n10. Реалтайм (WebSocket)")
    await websocket_check(
        client,
        base,
        tokens["manager"],
        tokens["engineer2"],
        created_users["manager"],
    )


async def websocket_check(
    client: httpx.AsyncClient,
    base: str,
    token: str,
    other_token: str,
    listener_id: str,
) -> None:
    import websockets

    ws_url = base.replace("http://", "ws://") + "/api/v1/ws?token=" + token

    async with websockets.connect(ws_url) as socket:
        ready = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        check("WebSocket принимает соединение", ready["type"] == "ready", str(ready))

        await socket.send(json.dumps({"type": "ping"}))
        pong = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        check("WebSocket отвечает на ping", pong["type"] == "pong", str(pong))

    async with websockets.connect(ws_url) as socket:
        await socket.recv()
        # Событие task.created получают автор, исполнитель и все, кому
        # задача видна, кроме того, кто её создал. Поэтому событие
        # создаёт другой сотрудник и назначает задачу слушателю.
        response = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Проверка реалтайма",
                "description": "Задача создаётся, чтобы дождаться события",
                "priority": "normal",
                "assignee_id": listener_id,
            },
            headers={"Authorization": f"Bearer {other_token}"},
        )
        check("Задача для проверки реалтайма создана", response.status_code == 201, response.text)
        try:
            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            check("Событие реалтайма приходит", "type" in event, str(event)[:120])
        except TimeoutError:
            check("Событие реалтайма приходит", False, "событие не пришло за 5 секунд")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
