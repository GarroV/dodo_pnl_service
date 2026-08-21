"""Доставка формы ролей из кода в базу — командой (T169, issue #126).

Тонкая обёртка вокруг `core.role_delivery`: вся логика решения — там, в
`verdict()` и `sync()`. Здесь только разбор аргументов, вызов и печать готового
отчёта. Собственного текста для человека команда не придумывает — `describe()`
уже пишет отчёт словами, и вторая формулировка того же самого разъехалась бы с
первой на первой же правке (то же рассуждение, что у `web/permissions.py`,
см. докстринг `role_delivery`).

Три режима:
- без флагов — довезти форму до базы: `sync(apply=True, adopt=False)`.
- `--check` — только вердикт, ни одной записи. Код возврата 1, если роли не
  сходятся с кодом — команда годится как предохранитель перед раскаткой,
  где молчаливое «вроде всё ок» дороже явного отказа.
- `--adopt` — объявить сегодняшнее состояние ролей без снимка формы
  поставленным продуктом. Это запись, и вместе с `--check` невозможна:
  `--check` обещает не трогать базу.

`role_delivery.DeliveryRefused` здесь не перехватывается: если доставка не
легла, это факт о состоянии стенда, а не сбой самой команды, и он обязан
уронить прогон трейсбеком, а не быть проглоченным.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from core.role_delivery import describe, sync


class Command(BaseCommand):
    help = "Довезти форму ролей из кода в базу и показать текущее расхождение"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help=(
                "ничего не писать в базу, только сверить с кодом; код выхода 1, "
                "если роли разъехались или сверка ничего не доказывает"
            ),
        )
        parser.add_argument(
            "--adopt",
            action="store_true",
            help=(
                "объявить нынешнее состояние ролей без снимка формы поставленным "
                "продуктом (нельзя вместе с --check — это уже запись в базу)"
            ),
        )

    def handle(self, *args, **options) -> None:
        check = options["check"]
        adopt = options["adopt"]

        if check and adopt:
            raise CommandError(
                "--check и --adopt вместе не имеют смысла: --adopt пишет в базу, "
                "а --check обещает этого не делать"
            )

        report = sync(connection, apply=not check, adopt=adopt)

        for line in describe(report):
            self.stdout.write(line)

        # Своей оговорки про необойдённые политики здесь нет намеренно: её
        # говорит `describe()` — первой строкой отчёта и вместо утверждения, что
        # роли совпадают. Раньше она стояла здесь, и отчёт получался такой:
        # «роли совпадают с кодом (0 шт.)», а через строку — «отчёт выше это не
        # доказывает». Человек запоминает первую строку.
        if check and not (report.in_sync and report.ready and report.bypasses_rls):
            raise SystemExit(1)
