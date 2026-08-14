"""Кто правил табель и когда — так, как это показывают человеку (T143).

Запись следа живёт в `store`, чтение — здесь. Разделены они не для красоты:
писать в табель будут три пути (экран, загрузка, коннектор Dodo IS), а читать
след — экран и, позже, сверка; общий модуль на то и другое пришлось бы звать из
`store`, который сам не знает ничего про показ.

**Имя человека спрашивается у базы, а не у таблицы `users`.** `select` на чужие
строки `users` роли приложения не выдан — там хэш пароля и почта, — и открывать
его ради одного поля нельзя. Ровно для этого в миграции `0042` заведена функция
`app_user_display_name(uuid)`: она отдаёт только имя и только тому, с кем у
человека есть общий партнёр. Тот же приём уже используется в истории переходов
расчёта (`payrun.lifecycle.history`).

**Пусто — это ответ.** Автора нет у ровной раскладки прежнего итога и у всего,
что пришло мимо продукта (сид, обслуживание). Продукт говорит об этом словами, а
не оставляет пустую подсказку: молчание читалось бы как поломка, а не как «этого
мы не знаем».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.db import connection

from core.models import TimesheetDay

__all__ = ["Author", "cell_authors", "display_names"]


@dataclass(frozen=True)
class Author:
    """Кто поставил число и когда. Имя уже готово к показу."""

    name: str
    at: datetime


def display_names(user_ids) -> dict[UUID, str]:
    """Имена людей для показа. Пустой список — ни одного запроса.

    Одним запросом на всю страницу, а не по имени на строку: в сетке 35 строк и
    шесть колонок, и вопрос «как зовут этого человека» повторяется в ней сотни
    раз при двух-трёх разных ответах.
    """
    wanted = sorted({user_id for user_id in user_ids if user_id})
    if not wanted:
        return {}
    with connection.cursor() as cursor:
        cursor.execute(
            "select u, app_user_display_name(u) from unnest(%s::uuid[]) as u",
            [[str(user_id) for user_id in wanted]],
        )
        return {row[0]: row[1] or "" for row in cursor.fetchall() if row[1]}


def cell_authors(tenant_id: UUID, period: date) -> dict[tuple[UUID, str], Author]:
    """Автор каждой ячейки часов месяца: ключ — «строка табеля + тип часов».

    Дни одного типа пишутся одной операцией и потому несут один и тот же след;
    берётся последний по времени — `distinct on` в самой базе, а не выборка всех
    дней в память: их у партнёра под пять тысяч на месяц, а ответов среди них
    несколько десятков.

    Что попадёт в выборку, решают политики базы: у управляющего в ней будут
    только его точки. Второго фильтра здесь нет намеренно — фильтр рядом с
    политикой и есть тот способ, которым доступ расходится сам с собой (D014).
    """
    rows = (
        TimesheetDay.objects.filter(
            tenant_id=tenant_id, timesheet__period=period, edited_by__isnull=False,
        )
        .order_by("timesheet_id", "hour_type", "-edited_at")
        .distinct("timesheet_id", "hour_type")
        .values_list("timesheet_id", "hour_type", "edited_by", "edited_at")
    )
    found = list(rows)
    names = display_names(user_id for _, _, user_id, _ in found)
    return {
        (timesheet_id, hour_type): Author(name=names[user_id], at=edited_at)
        for timesheet_id, hour_type, user_id, edited_at in found
        # Имени нет — значит человек уже не в этом партнёре и назвать его нечем.
        # Показывать голый идентификатор было бы ответом ни на что.
        if user_id in names
    }
