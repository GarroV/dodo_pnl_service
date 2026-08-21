"""Карта маршрутов продукта: адрес → имя → модуль с кодом (issue #134).

Зачем команда, а не документ, написанный руками. Маршрутов больше семидесяти, и
они растут с каждой очередью. Список, поддерживаемый вручную, отстаёт от кода
молча, а врущая карта хуже отсутствующей: по ней человек идёт не в тот файл и
делает вывод, что кода нет. Поэтому источник здесь один — сам резолвер Django,
то есть ровно то, что продукт отдаёт в проде.

Готовый вид карты лежит в `docs/routes.md` и сверяется тестом
`tests/test_routes_doc.py`: добавили маршрут, не обновив карту, — прогон красный.
Тот же приём, что у `test_readme_numbers` с числом тестов.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.urls import URLPattern, URLResolver, get_resolver

HEADER = """<!-- СОБИРАЕТСЯ КОМАНДОЙ, РУКАМИ НЕ ПРАВИТЬ -->
<!-- Пересобрать: python manage.py routes --write -->

# Карта маршрутов

Адрес в браузере → имя маршрута → модуль, где лежит код. Нужна для одного:
понять, какой файл открыть, чтобы поправить конкретный экран, не вычитывая
проект целиком.

Собирается из резолвера Django командой `python manage.py routes`, то есть
описывает то, что продукт отдаёт на самом деле. Свежесть держит тест
`tests/test_routes_doc.py`.

| Адрес | Имя | Код |
|---|---|---|
"""


def collect(resolver=None, prefix: str = "") -> list[tuple[str, str, str]]:
    """Все маршруты продукта плоским списком, вглубь по вложенным резолверам."""
    resolver = resolver or get_resolver()
    found: list[tuple[str, str, str]] = []
    for entry in resolver.url_patterns:
        pattern = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            found.extend(collect(entry, pattern))
            continue
        if not isinstance(entry, URLPattern):  # pragma: no cover — защита от новых типов
            continue
        view = entry.callback
        # У вью на классах человеку нужен класс, а не сгенерированная обёртка:
        # искать он будет `PeriodView`, а не `view` внутри `as_view`.
        owner = getattr(view, "view_class", view)
        module = getattr(owner, "__module__", "?")
        name = getattr(owner, "__qualname__", getattr(owner, "__name__", "?"))
        found.append((pattern, entry.name or "—", f"{module}.{name}"))
    return found


def as_markdown() -> str:
    rows = sorted(set(collect()))
    body = "".join(f"| `/{a}` | `{n}` | `{c}` |\n" for a, n, c in rows)
    return HEADER + body


class Command(BaseCommand):
    help = "Карта маршрутов: адрес → имя → модуль. С --write пишет docs/routes.md."

    def add_arguments(self, parser):
        parser.add_argument(
            "--write", action="store_true",
            help="перезаписать docs/routes.md вместо печати в консоль",
        )

    def handle(self, *args, **options):
        from pathlib import Path

        text = as_markdown()
        if not options["write"]:
            self.stdout.write(text)
            return
        # Корень репозитория: файл лежит в src/core/management/commands/, то
        # есть четыре уровня вверх — commands → management → core → src → корень.
        target = Path(__file__).resolve().parents[4] / "docs" / "routes.md"
        target.write_text(text, encoding="utf-8")
        self.stdout.write(f"записано: {target}")
