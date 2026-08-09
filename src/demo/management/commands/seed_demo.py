"""
`manage.py seed_demo` — наполнить демо-базу.

Команда не делает ничего, пока не убедится, что подключена к демо-базе
(`demo.guard`). Причина в цене ошибки: демо сбрасывается пересозданием базы
целиком, а рядом живёт база с ФИО и суммами живых людей.

`--reset` — полный путь: пересобрать эталон и восстановить демо из него. Ровно
то, что делает служба сброса по расписанию, только руками.
"""
from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from demo import reset as demo_reset
from demo.guard import DemoGuardRefused, require_demo_dsn
from demo.seed import ROLES, seed_demo


def current_dsn() -> str:
    """Куда подключено приложение прямо сейчас — из настроек, а не из окружения.

    Именно из настроек: `DATABASE_URL` может быть переопределён при запуске, и
    сверять надо то, куда пойдёт запись, а не то, что написано в файле.
    """
    db = settings.DATABASES["default"]
    port = f":{db['PORT']}" if db.get("PORT") else ""
    return f"postgresql://{db['HOST']}{port}/{db['NAME']}"


class Command(BaseCommand):
    help = "Наполнить демо-базу выдуманными англоязычными данными (тенант demo)"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="пересобрать эталон и восстановить демо-базу из него (D016)",
        )

    def handle(self, *args, **options) -> None:
        if options["reset"]:
            self._reset()
            return

        try:
            require_demo_dsn(current_dsn(), os.environ)
        except DemoGuardRefused as refusal:
            raise CommandError(str(refusal)) from refusal

        result = seed_demo(log=lambda text: self.stdout.write(f"  {text}"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Демо готово: тенант {result['tenant'].code}, "
                f"{result['people']} сотрудников, "
                f"периоды {', '.join(f'{p:%Y-%m}' for p in result['periods'])}"
            )
        )
        self.stdout.write(f"Учётки демо (пароль у всех: {result['password']}):")
        for code, title, ledgers, unit, _permissions in ROLES:
            self.stdout.write(
                f"  {code:<11} {title}: регистры {','.join(ledgers)}"
                + (f", точка {unit}" if unit else ", все точки")
            )

    def _reset(self) -> None:
        try:
            demo_reset.rebuild_template(log=lambda text: self.stdout.write(f"  {text}"))
            demo_reset.restore(log=lambda text: self.stdout.write(f"  {text}"))
        except DemoGuardRefused as refusal:
            raise CommandError(str(refusal)) from refusal
        self.stdout.write(self.style.SUCCESS("Демо пересобрано из эталона."))
