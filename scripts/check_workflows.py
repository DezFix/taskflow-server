"""Ищет в workflow-файлах значения с двоеточием без кавычек.

Такой текст YAML считает отображением, и GitHub отклоняет файл целиком:
сбой происходит за секунды и не показывает, в чём дело.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOTS = [
    Path(r"D:\CODE\Task-work\taskflow\.github\workflows"),
    Path(r"D:\CODE\Task-work\taskflow-server\.github\workflows"),
]


def check(path: Path) -> list[str]:
    problems: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if line.startswith("#") or ":" not in line:
            continue

        # Отрезаем ведущий "- " маркера списка, затем делим по первому
        # двоеточию: слева ключ, справа значение.
        body = raw[2:] if raw.lstrip().startswith("- ") else raw
        head, sep, value = body.partition(":")
        if not sep:
            continue
        value = value.strip()
        if not value:
            continue

        # Значение с двоеточием и пробелом внутри, без кавычек:
        # YAML примет его за вложенное отображение.
        if ": " in value and not (value.startswith('"') or value.startswith("'")):
            problems.append(f"{path.name}:{number}: {line}")
    return problems


def main() -> int:
    found: list[str] = []
    for root in ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.yml")):
            found.extend(check(path))
            try:
                yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                found.append(f"{path.name}: YAML не разбирается: {exc}")

    if found:
        print("Требует кавычек:")
        for line in found:
            print(f"  {line}")
        return 1

    print("Все workflow-файлы разбираются")
    return 0


if __name__ == "__main__":
    sys.exit(main())
