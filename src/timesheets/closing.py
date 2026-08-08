"""Закрытие часов по точке: состояние, отказ словами и два действия (T022).

Порядок тот же, что у прав роли и видимости регистров: **гарантирует база**
(политики в миграции `0031`), **объясняет приложение**. Второго движка доступа
здесь нет — есть чтение состояния и формулировки, которых у базы быть не может:
она отвечает кодом ошибки, из которого человеку ничего не понятно.

Закрытие относится к паре «точка + месяц». Одна точка закрывается независимо от
соседних: это прямое требование спеки («хочу закрывать свою точку независимо от
других, чтобы не ждать всю сеть») и разница между этим действием и утверждением
периода, которое замораживает расчёт целиком (T023, T025).

**Строки табеля без точки не закрываются никогда.** Точка обнуляется, когда
пиццерию удаляют (`on delete set null`), и такая строка не принадлежит ни одной
точке — закрыть её было бы нечем, а прятать от правки значило бы потерять часы
молча. То же правило записано в `timesheet_closed()`: `unit_id is null` не
совпадёт ни с одним закрытием.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.utils.timezone import now

from core.models import Timesheet, TimesheetClosure, Unit
from web.views import month_title

__all__ = [
    "ClosureRefused", "UnitState", "close_unit", "is_closed", "open_closures",
    "refuse_if_closed", "reopen_unit", "unit_states",
]


class ClosureRefused(Exception):
    """Часы точки закрыты, и человеку сказано, что с этим делать.

    409, а не 403: право у человека, скорее всего, есть — не подходит состояние.
    Тот же код, которым отвечает отказ расчёта (`PayrunRefused`), и та же
    причина: по коду ответа снаружи должно быть видно, что делать дальше.
    """

    http_status = 409

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class UnitState:
    """Точка на экране табеля: закрыта или нет и что с ней можно сделать."""

    unit_id: UUID
    code: str
    title: str
    rows: int
    closed_at: datetime | None = None

    @property
    def closed(self) -> bool:
        return self.closed_at is not None


def open_closures(tenant_id: UUID, period: date) -> dict[UUID, TimesheetClosure]:
    """Действующие закрытия месяца — по точкам.

    Что видно, решают политики: у управляющего в ответе будет только его точка.
    Приложение выборку не сужает — второй фильтр рядом с политикой и есть тот
    способ, которым доступ расходится сам с собой (D014).
    """
    return {
        closure.unit_id: closure
        for closure in TimesheetClosure.objects.filter(
            tenant_id=tenant_id, period=period, reopened_at__isnull=True
        )
    }


def is_closed(tenant_id: UUID, unit_id: UUID | None, period: date) -> bool:
    if unit_id is None:
        return False
    return unit_id in open_closures(tenant_id, period)


def refusal(unit_code: str, period: date) -> str:
    return (
        f"Часы точки {unit_code} за {month_title(period)} закрыты. "
        "Чтобы их править, точку нужно открыть заново."
    )


def refuse_if_closed(row: Timesheet) -> None:
    """Отказать до записи, если часы этой строки закрыты.

    База отвергнет запись и без этого — но ошибкой политики, из которой человеку
    ничего не понятно. Порядок тот же, что с регистрами учёта и утверждённым
    расчётом: объясняет приложение, гарантирует база.
    """
    if not is_closed(row.tenant_id, row.unit_id, row.period):
        return
    code = row.unit.code if row.unit_id else ""
    raise ClosureRefused(refusal(code, row.period))


def unit_states(tenant_id: UUID, period: date, rows) -> list[UnitState]:
    """Точки, которые видно на этом табеле, и состояние каждой.

    Список строится **из строк сетки**, а не из справочника точек: экран
    предлагает закрыть ровно то, что на нём показано. Точка без единой строки
    табеля в этом месяце в панели не появляется — закрывать в ней нечего, а
    кнопка, которая ничего не меняет, читается как поломка.
    """
    closures = open_closures(tenant_id, period)
    seen: dict[UUID, int] = {}
    for row in rows:
        if row.unit_id is not None:
            seen[row.unit_id] = seen.get(row.unit_id, 0) + 1

    units = {unit.id: unit for unit in Unit.objects.filter(pk__in=list(seen))}
    states = [
        UnitState(
            unit_id=unit_id,
            code=units[unit_id].code if unit_id in units else "",
            title=units[unit_id].title if unit_id in units else "",
            rows=count,
            closed_at=(
                closures[unit_id].closed_at if unit_id in closures else None
            ),
        )
        for unit_id, count in seen.items()
    ]
    return sorted(states, key=lambda state: state.code)


def close_unit(*, tenant_id: UUID, unit_id: UUID, period: date,
               actor_id: UUID | None) -> None:
    """Закрыть часы точки за месяц. Повторное закрытие ничего не меняет.

    Идемпотентность здесь не украшение: у частичного уникального индекса
    («одно действующее закрытие на точку и месяц») вторая попытка иначе упала бы
    ошибкой базы там, где человек просто нажал кнопку дважды.
    """
    if is_closed(tenant_id, unit_id, period):
        return
    TimesheetClosure.objects.create(
        tenant_id=tenant_id, unit_id=unit_id, period=period, closed_by=actor_id
    )


def reopen_unit(*, tenant_id: UUID, unit_id: UUID, period: date,
                actor_id: UUID | None) -> None:
    """Открыть часы точки заново.

    Закрытие не удаляется, а помечается: история закрытий — единственный ответ
    на вопрос «когда точка была готова», и стирать его нельзя.

    Причина не требуется — в отличие от отката утверждённого периода (T025), где
    переписывается уже выданная ведомость. Здесь закрытие всего лишь говорит
    бухгалтеру «я закончил вводить», и человек, нажавший кнопку по ошибке, не
    должен ждать, пока его откроют обратно.
    """
    TimesheetClosure.objects.filter(
        tenant_id=tenant_id, unit_id=unit_id, period=period, reopened_at__isnull=True
    ).update(reopened_at=now(), reopened_by=actor_id)
