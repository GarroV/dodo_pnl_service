"""
`manage.py demo_reset` — вернуть демо-базу к эталону.

Без ключей — один сброс. С `--every N` — служба сброса: та самая, которая
крутится в compose и не даёт демо протухнуть. Ручной кнопки в интерфейсе нет
намеренно: демо, которое чинят руками, чинят ровно до первого дня, когда на него
никто не смотрит.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from demo import reset as demo_reset
from demo.guard import DemoGuardRefused


class Command(BaseCommand):
    help = "Пересоздать демо-базу из эталона (D016)"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--every", type=int, default=0, metavar="МИНУТ",
            help="крутиться службой и сбрасывать демо каждые N минут",
        )
        parser.add_argument(
            "--rebuild-template", action="store_true",
            help="сначала собрать эталон заново (нужно после выкладки новой версии)",
        )

    def handle(self, *args, **options) -> None:
        say = lambda text: self.stdout.write(f"  {text}")  # noqa: E731

        try:
            if options["rebuild_template"]:
                demo_reset.rebuild_template(log=say)
            if options["every"]:
                # Первый сброс — сразу, не через N минут: служба, поднятая на
                # пустой базе, обязана дать работающее демо немедленно.
                demo_reset.restore(log=say)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Демо сброшено. Дальше — каждые {options['every']} мин."
                    )
                )
                demo_reset.run_every(options["every"], log=say)
                return
            demo_reset.restore(log=say)
        except DemoGuardRefused as refusal:
            raise CommandError(str(refusal)) from refusal

        self.stdout.write(self.style.SUCCESS("Демо пересоздано из эталона."))
