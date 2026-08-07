"""Первичная загрузка правил страны из YAML в таблицу `rule_presets`.

Файлы остаются источником только для этого шага: дальше расчёт читает правила
из базы, а партнёр меняет их переопределениями, не трогая файл.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from core.rules import import_presets
from payroll import list_presets


class Command(BaseCommand):
    help = "Загрузить пресеты правил стран из YAML в таблицу rule_presets"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "codes", nargs="*",
            help=f"какие пресеты грузить; без аргументов — все ({', '.join(list_presets())})",
        )

    def handle(self, *args, **options) -> None:
        loaded = import_presets(options["codes"] or None)
        self.stdout.write(self.style.SUCCESS(f"Загружено пресетов: {len(loaded)}"))
        for code in loaded:
            self.stdout.write(f"  {code}")
