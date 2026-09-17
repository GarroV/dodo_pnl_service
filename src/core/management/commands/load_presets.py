"""Первичная загрузка правил страны из YAML в таблицу `rule_presets`.

Файлы остаются источником только для этого шага: дальше расчёт читает правила
из базы, а партнёр меняет их переопределениями, не трогая файл.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.rules import import_presets_detailed
from payroll import list_presets
from payroll.frames import FrameMisconfigured


class Command(BaseCommand):
    help = "Загрузить пресеты правил стран из YAML в таблицу rule_presets"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "codes", nargs="*",
            help=f"какие пресеты грузить; без аргументов — все ({', '.join(list_presets())})",
        )

    def handle(self, *args, **options) -> None:
        # Неприменимая рамка — отказ словами и ненулевой код возврата, а не
        # трассировка (T192). Загрузку зовут из скриптов развёртывания, и
        # трассировка там читается как «упало что-то в Django», а не как
        # «поправь опечатку в YAML вот в этой строке».
        try:
            result = import_presets_detailed(options["codes"] or None)
        except FrameMisconfigured as broken:
            raise CommandError(str(broken)) from broken
        self.stdout.write(self.style.SUCCESS(f"Загружено пресетов: {len(result.loaded)}"))
        for code in result.loaded:
            self.stdout.write(f"  {code}")
        # Пропущенное говорится вслух и предупреждением, а не строкой в общем
        # списке (T165). Версия, которую правили в продукте, файлом не
        # перезаписывается — иначе повторный прогон откатил бы правку и молчал
        # бы об этом. Молча пропустить здесь хуже, чем отказать: человек ушёл бы
        # с уверенностью, что база теперь как файл.
        if result.skipped:
            self.stdout.write(self.style.WARNING(
                f"Не тронуто (правились в продукте): {len(result.skipped)}"
            ))
            for code in result.skipped:
                self.stdout.write(f"  {code} — версия правилась на экране правил страны")
            self.stdout.write(
                "Файл поверх такой версии не кладётся. Нужно вернуть файл — "
                "заведите новую версию с новой датой на экране правил страны."
            )
