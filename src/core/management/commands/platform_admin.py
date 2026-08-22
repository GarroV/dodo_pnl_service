"""Право вести правила стран: выдать, отобрать, перечислить (T165).

Почему командой, а не экраном. Право не партнёрское: тело пресета страны общее
для всех партнёров этой страны, и выдавать его внутри тенанта нельзя — партнёр
получил бы возможность менять расчёт соседу. Значит выдаёт его тот, кто
управляет системой целиком, тем же путём, каким появляется первая учётка:
командой на сервере. Из приложения таблица `platform_admins` не пишется вовсе —
ни одной политики на запись у неё нет, а права роли `app_user` на них отозваны
(миграция `0248`).

Команда ходит в базу владельцем схемы (обычный `DATABASE_URL` миграций), потому
что политики роли приложения её бы и не пустили. Это не обход разграничения, а
его устройство: у права должен быть источник вне продукта, иначе первый
администратор платформы неоткуда взяться.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import PlatformAdmin, User


class Command(BaseCommand):
    help = "Выдать или отобрать право вести правила стран"

    def add_arguments(self, parser) -> None:
        parser.add_argument("username", nargs="?", help="логин учётки")
        parser.add_argument(
            "--revoke", action="store_true", help="отобрать право вместо выдачи",
        )
        parser.add_argument(
            "--note", default="", help="зачем выдано: повод и кто выдал",
        )
        parser.add_argument(
            "--list", action="store_true", dest="show", help="перечислить, у кого право есть",
        )

    def handle(self, *args, **options) -> None:
        if options["show"]:
            self._show()
            return

        username = options["username"]
        if not username:
            raise CommandError("нужен логин учётки либо --list")

        user = User.objects.filter(username=username).first()
        if user is None:
            # Молча ничего не делать нельзя: человек ушёл бы с уверенностью, что
            # право выдано, а опечатка в логине осталась бы незамеченной.
            raise CommandError(f"учётки «{username}» нет")

        if options["revoke"]:
            removed, _details = PlatformAdmin.objects.filter(user=user).delete()
            if removed:
                self.stdout.write(self.style.SUCCESS(f"Право отобрано: {username}"))
            else:
                self.stdout.write(f"У {username} этого права и не было")
            return

        _row, created = PlatformAdmin.objects.get_or_create(
            user=user, defaults={"note": options["note"]},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Право выдано: {username}"))
        else:
            self.stdout.write(f"У {username} право уже есть")

    def _show(self) -> None:
        rows = list(PlatformAdmin.objects.select_related("user").order_by("granted_at"))
        if not rows:
            self.stdout.write(
                "Права вести правила стран нет ни у кого. Правила страны сейчас "
                "меняются только файлом и командой load_presets."
            )
            return
        self.stdout.write(f"Вправе вести правила стран: {len(rows)}")
        for row in rows:
            note = f" — {row.note}" if row.note else ""
            self.stdout.write(f"  {row.user.username} (с {row.granted_at:%Y-%m-%d}){note}")
