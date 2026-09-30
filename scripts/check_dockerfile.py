"""Проверяет, что Dockerfile разбирается docker-сборщиком.

Ошибка в первой же строке (например, описание образа вместо `FROM`)
обрывает всю CI-сборку с невнятным «unknown instruction».
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(r"D:\CODE\Task-work\taskflow-server")
DOCKERFILE = ROOT / "Dockerfile"

DIRECTIVES = {
    "FROM", "RUN", "CMD", "LABEL", "EXPOSE", "ENV", "ADD", "COPY",
    "ENTRYPOINT", "VOLUME", "USER", "WORKDIR", "ARG", "ONBUILD",
    "STOPSIGNAL", "HEALTHCHECK", "SHELL",
}


def main() -> int:
    if not DOCKERFILE.exists():
        print("Dockerfile не найден")
        return 0

    problems: list[str] = []
    seen_from = False
    continued = False

    for number, raw in enumerate(DOCKERFILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        # Продолжение предыдущей директивы: строки не самостоятельны.
        if continued:
            continued = line.endswith("\\")
            continue

        word = line.split()[0].upper()
        if word in DIRECTIVES:
            if word == "FROM":
                seen_from = True
            continued = line.endswith("\\")
            continue

        # Не директива и не продолжение — почти всегда ошибка формата.
        problems.append(f"{number}: {line}")

    if not seen_from:
        problems.append("нет ни одной директивы FROM")

    if problems:
        print("Dockerfile не разбирается:")
        for line in problems:
            print(f"  {line}")
        return 1

    print("Dockerfile разбирается")
    return 0


if __name__ == "__main__":
    sys.exit(main())
