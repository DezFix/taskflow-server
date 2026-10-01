"""Каталог прав и системные роли.

Права описаны строковыми константами: они попадают и в БД, и в OpenAPI,
и в клиент, поэтому должны быть стабильными и читаемыми.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Permission:
    key: str
    title: str
    group: str
    description: str = ""


def _p(key: str, title: str, group: str, description: str = "") -> Permission:
    return Permission(key=key, title=title, group=group, description=description)


ALL_PERMISSIONS: tuple[Permission, ...] = (
    # --- Сотрудники ---
    _p("users.view", "Просмотр сотрудников", "Сотрудники"),
    _p("users.create", "Создание сотрудников", "Сотрудники"),
    _p("users.edit", "Редактирование сотрудников", "Сотрудники"),
    _p("users.delete", "Удаление сотрудников", "Сотрудники"),
    _p("users.reset_password", "Сброс пароля сотрудника", "Сотрудники"),
    # --- Роли ---
    _p("roles.view", "Просмотр ролей", "Роли"),
    _p("roles.create", "Создание ролей", "Роли"),
    _p("roles.edit", "Редактирование ролей", "Роли"),
    _p("roles.delete", "Удаление ролей", "Роли"),
    # --- Задачи ---
    _p("tasks.view", "Просмотр задач", "Задачи", "Видеть задачи, где вы автор или исполнитель"),
    _p("tasks.view_all", "Все задачи отдела", "Задачи", "Видеть задачи любых сотрудников"),
    _p("tasks.create", "Создание задач", "Задачи"),
    _p("tasks.edit_any", "Редактирование любых задач", "Задачи"),
    _p("tasks.edit_assigned", "Редактирование своих задач", "Задачи"),
    _p("tasks.delete", "Удаление задач", "Задачи"),
    _p("tasks.assign", "Назначение исполнителей", "Задачи"),
    _p("tasks.reports", "Отчёты по задачам", "Задачи"),
    # --- Чат ---
    _p("chat.direct", "Личные диалоги", "Чат"),
    _p("chat.group", "Групповые чаты", "Чат"),
    _p("chat.group_create", "Создание групп", "Чат"),
    # --- Файлы ---
    _p("files.upload", "Загрузка файлов", "Файлы"),
    _p("files.view_all", "Просмотр всех файлов", "Файлы"),
    # --- Голос ---
    _p("voice.transcribe", "Распознавание голоса", "Голос"),
    # --- Настройки ---
    _p("settings.view", "Просмотр настроек", "Настройки"),
    _p("settings.edit", "Изменение настроек", "Настройки"),
    _p("settings.manage_roles", "Управление ролями", "Настройки"),
    _p("settings.backup", "Резервные копии", "Настройки"),
    _p("settings.audit_log", "Журнал действий", "Настройки"),
)

PERMISSION_KEYS: frozenset[str] = frozenset(p.key for p in ALL_PERMISSIONS)
PERMISSION_TITLES: dict[str, str] = {p.key: p.title for p in ALL_PERMISSIONS}
PERMISSION_BY_KEY: dict[str, Permission] = {p.key: p for p in ALL_PERMISSIONS}

_ADMIN_KEY = "Администратор"
_HEAD_KEY = "Глава отдела"
_STAFF_KEY = "Сотрудник"

#: Машиночитаемые идентификаторы системных ролей.
#:
#: Ключ роли в базе русский: он исторически используется в проверках прав
#: и в существующих базах, менять его нельзя. Но интерфейсу нужен
#: независимый от языка идентификатор — по нему клиент показывает
#: собственное название роли на языке сотрудника.
SYSTEM_ROLE_IDS: dict[str, str] = {
    "admin": _ADMIN_KEY,
    "head": _HEAD_KEY,
    "staff": _STAFF_KEY,
}

_ID_BY_ROLE_KEY: dict[str, str] = {v: k for k, v in SYSTEM_ROLE_IDS.items()}


def system_role_id(role_key: str) -> str | None:
    """Возвращает английский идентификатор системной роли.

    Для своих ролей возвращает None: их названия задаёт администратор,
    и переводить их нельзя.
    """
    return _ID_BY_ROLE_KEY.get(role_key)


#: Системные роли, создаются при первом запуске, удалению не подлежат.
SYSTEM_ROLES: dict[str, tuple[str, bool, list[str]]] = {
    _ADMIN_KEY: (
        "Полный доступ ко всем разделам, включая системные настройки",
        True,
        sorted(PERMISSION_KEYS),
    ),
    _HEAD_KEY: (
        "Управление отделом: сотрудники, задачи, отчёты. Без системных настроек",
        True,
        sorted(key for key in PERMISSION_KEYS if not key.startswith("settings.manage_roles")),
    ),
    _STAFF_KEY: (
        "Свои задачи, чат и профиль",
        True,
        [
            "users.view",
            "roles.view",
            "tasks.view",
            "tasks.edit_assigned",
            "tasks.create",
            "tasks.assign",
            "tasks.reports",
            "chat.direct",
            "chat.group",
            "files.upload",
        ],
    ),
}

SYSTEM_ROLE_KEYS: tuple[str, ...] = (_ADMIN_KEY, _HEAD_KEY, _STAFF_KEY)


def default_permissions_for(role_key: str) -> list[str]:
    return list(SYSTEM_ROLES.get(role_key, (None, False, []))[2])
