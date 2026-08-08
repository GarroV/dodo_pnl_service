"""Пропущенный тест — не проверенный тест. Здесь решается, какие пропуски норма.

Зачем это вообще нужно. Тесты схемы и доступа работают на живом Postgres: они
создают временную базу, накатывают миграции и проверяют политики RLS. Нет базы —
они не падают, а пропускаются, и прогон остаётся зелёным, не проверив
разграничение доступа вообще. На этом проекте так уже прожил незамеченным дефект
видимости регистров, поэтому «зелёный прогон» без проверки состава пропусков
здесь ничего не значит.

Что считается нормой: **только** сверка с настоящей таблицей бухгалтерии
партнёра. Файла с ФИО и суммами живых людей в репозитории нет и не будет
(решение D028), поэтому эти тесты пропускаются всегда и везде, где нет
PAYROLL_FIXTURE. Любой другой пропуск — повод остановить сборку и посмотреть,
что именно перестало проверяться.

Дополнительно проверяется, что тесты схемы действительно шли: пустой список
пропусков может означать и «всё хорошо», и «этих тестов вообще не собрали».
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Единственная разрешённая причина пропуска — отсутствие настоящей таблицы.
# Сверяем по имени переменной: обе формулировки в conftest.py её называют.
ALLOWED_MARKER = "PAYROLL_FIXTURE"

# Файлы, которые обязаны дать хотя бы один пройденный тест: они и есть проверка
# схемы и разграничения доступа на живой базе.
REQUIRED_FILES = ("test_schema_access", "test_plain_owner")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("нужен один аргумент: путь к junit-отчёту pytest", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"нет отчёта {path}: pytest не дошёл до его записи", file=sys.stderr)
        return 1

    root = ET.parse(path).getroot()
    cases = root.iter("testcase")

    bad_skips: list[tuple[str, str]] = []
    allowed_skips = 0
    ran_by_file: dict[str, int] = {name: 0 for name in REQUIRED_FILES}

    for case in cases:
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        skipped = case.find("skipped")
        if skipped is None:
            for required in REQUIRED_FILES:
                if required in (case.get("classname") or ""):
                    ran_by_file[required] += 1
            continue

        # Только message: в text pytest повторяет ту же строку с путём и
        # номером, и в логе причина двоилась бы на каждый тест.
        reason = (skipped.get("message") or skipped.text or "").strip()
        if ALLOWED_MARKER in reason:
            allowed_skips += 1
        else:
            bad_skips.append((name, reason))

    print(f"пропущено по {ALLOWED_MARKER} (это норма, D028): {allowed_skips}")
    for required, count in ran_by_file.items():
        print(f"пройдено в {required}: {count}")

    problems: list[str] = []
    if bad_skips:
        # Причины у таких пропусков почти всегда одинаковые (нет базы), поэтому
        # печатаем список имён и причины отдельным коротким набором: полный
        # список пар на три десятка тестов читать невозможно.
        names = "\n".join(f"  {name}" for name, _ in bad_skips)
        reasons = "\n".join(f"  — {reason}" for reason in sorted({r for _, r in bad_skips}))
        problems.append(
            f"пропущено {len(bad_skips)} тестов, которые пропускаться не должны "
            "(чаще всего это значит, что не было живого Postgres):\n"
            f"{names}\nпричины:\n{reasons}"
        )
    empty = [name for name, count in ran_by_file.items() if count == 0]
    if empty:
        problems.append(
            "ни одного пройденного теста в: "
            + ", ".join(empty)
            + " — проверка схемы и доступа не выполнялась"
        )

    if problems:
        print("\nПРОВЕРКА ПРОПУСКОВ НЕ ПРОЙДЕНА", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    print("состав пропусков в порядке")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
