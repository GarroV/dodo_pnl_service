"""Карта маршрутов в `docs/routes.md` не отстала от кода (issue #134).

Почему это тест, а не забота человека. Карта нужна ровно для того, чтобы по ней
находили файл, который надо поправить. Карта, отставшая на один маршрут, ведёт
человека не в тот файл — и он делает вывод, что кода нет вовсе. То есть
устаревшая карта вреднее отсутствующей, и держать её свежесть на внимании нельзя.

Красный здесь — не поломка продукта: скорее всего добавили или убрали маршрут.
Пересоберите карту одной командой:

    python manage.py routes --write
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "routes.md"


def test_the_route_map_matches_the_code():
    from core.management.commands.routes import as_markdown

    assert DOC.exists(), f"нет карты маршрутов {DOC.relative_to(ROOT)}"
    written = DOC.read_text(encoding="utf-8")
    fresh = as_markdown()
    if written == fresh:
        return

    # Разницу показываем строками, а не «файлы не совпали»: человеку нужно
    # знать, какой именно маршрут появился или исчез, иначе он пойдёт сравнивать
    # семьдесят строк глазами.
    was = {line for line in written.splitlines() if line.startswith("| `/")}
    now = {line for line in fresh.splitlines() if line.startswith("| `/")}
    added = sorted(now - was)
    gone = sorted(was - now)
    detail = ""
    if added:
        detail += "\nв коде есть, в карте нет:\n" + "\n".join(added)
    if gone:
        detail += "\nв карте есть, в коде нет:\n" + "\n".join(gone)
    if not detail:
        detail = "\nсписок маршрутов совпал, разошёлся текст вокруг таблицы"
    raise AssertionError(
        "карта маршрутов разошлась с кодом — пересоберите "
        "`python manage.py routes --write`" + detail
    )
