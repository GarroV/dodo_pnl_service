import sys

from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.db.models.signals import post_migrate


class CoreConfig(AppConfig):
    """Доменная схема: тенанты, оргструктура, справочники, зарплатные таблицы."""

    name = "core"
    verbose_name = "Ядро"

    def ready(self) -> None:
        # Без этого массивы enum-типов приезжают из базы строкой, а не списком —
        # см. core/db_types.py.
        from .db_types import on_connection_created

        connection_created.connect(on_connection_created, dispatch_uid="core.enum_types")

        # Форма ролей едет вместе с кодом, а не отдельной командой «не забыть».
        post_migrate.connect(
            deliver_role_shapes, sender=self, dispatch_uid="core.role_delivery",
        )


def deliver_role_shapes(sender, **kwargs) -> None:
    """Довезти форму ролей до базы сразу после `migrate` (T169, issue #126).

    **Почему на сигнал, а не миграцией.** Миграция выполняется один раз и
    остаётся применённой — следующая правка `ROLE_SHAPES` через неё уже никуда
    не поедет. Именно поэтому право приходилось довозить новой миграцией каждый
    раз (`0110`, `0220`, `0230`), и забытая миграция молчала. Деплой — это
    `migrate`, и форма обязана ехать тем же шагом, а не рядом с ним.

    **Почему не при старте приложения.** Старт бывает у каждого процесса, в том
    числе у рабочего процесса очереди и у команды `shell`; запись схемы данных
    из любого из них — это гонка на пустом месте. `migrate` же ровно один и
    выполняется ролью, у которой на это есть права.

    **Что здесь НЕ подавляется.** `DeliveryRefused` — фактически непроведённая
    доставка (запись отрезана политиками, снимок не лёг) — валит `migrate`
    нарочно: «миграции прошли, права не приехали» это худший из исходов, ровно
    тот, что уже случался (issue #44). А вот отсутствие колонки и роль без
    обхода RLS — не отказ, а сообщение словами: первое бывает при `migrate core
    <номер>` до `0245`, второе разбирается в `role_delivery`. Про роль без
    обхода политик говорит сам отчёт (`describe`), первой строкой и вместо
    утверждения о согласии, — здесь для этого ничего делать не нужно, кроме
    того, чтобы отчёт в таком случае напечатать.
    """
    from django.db import connections

    from .role_delivery import describe, sync

    verbosity = int(kwargs.get("verbosity", 1))
    using = kwargs.get("using") or "default"
    stream = kwargs.get("stdout") or sys.stdout

    def say(line: str) -> None:
        if verbosity >= 1:
            stream.write(f"{line}\n")

    report = sync(connections[using], apply=True, say=say)
    # Отчёт — только когда есть что сказать: «роли совпадают с кодом» в конце
    # каждого migrate превратилось бы в строку, которую перестают читать.
    # `recorded` здесь обязателен: без него первый прогон на базе, где роли
    # случайно совпали с кодом, ЗАПИСЫВАЛ снимки во все такие роли и не говорил
    # об этом ни строки. Значения при этом верные, неправды нет — но «сделали и
    # не сказали» противоречит контракту самого отчёта («что доставка увидела и
    # что сделала»), а наблюдаемость первого боевого прогона тут и есть главное.
    if verbosity >= 1 and (
        report.delivered
        or report.recorded
        or report.adopted
        or not report.in_sync
        or not report.bypasses_rls
    ):
        for line in describe(report):
            stream.write(f"Роли: {line}\n")
