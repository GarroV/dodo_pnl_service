"""
Сетка «сотрудник × тип часа»: что показать на экране.

Колонки приходят из правил страны, а не из списка в коде: новая страна не должна
требовать правки интерфейса. Строки — табели периода, по одной на пару
«сотрудник + точка»: человек, отработавший месяц на двух точках, это две строки,
и склеивать их нельзя — расчёт их тоже не склеивает.

Итоги считаются по показанным строкам (D023): управляющий видит сумму часов
своих точек, а не общую, из которой вычитанием выводится чужая.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

from core.models import Timesheet
from payroll import insured_base

from .closing import open_closures
from .store import country_of, hour_types

__all__ = ["Column", "Grid", "Row", "build_grid"]


@dataclass(frozen=True)
class Column:
    code: str
    title: str
    # Процент от часовой ставки — подсказка в заголовке. Показывается, потому
    # что «больничный» и «65% ставки» для бухгалтера одно и то же знание, но
    # второе он проверяет глазами, а первое помнит.
    pay_percent: Decimal | None = None
    unverified: bool = False


@dataclass
class Row:
    timesheet_id: UUID
    employee: str
    external_id: str
    unit: str
    norm_hours: Decimal
    insured_hours: Decimal
    # Сколько часов строки входит в базу для взносов по правилам страны.
    # Показывается не всегда, а когда расходится с самой базой: два числа в
    # одной колонке без причины только мешают читать столбик.
    insured_declared: Decimal = Decimal("0")
    cells: dict[str, Decimal] = field(default_factory=dict)
    # Закрыты ли часы этой строки (T022). Свойство строки, а не сетки: закрытие
    # идёт по точкам, и на одном экране соседствуют закрытая точка и открытая —
    # в этом весь смысл «спорная точка не держит остальные».
    unit_id: UUID | None = None
    closed: bool = False

    @property
    def total(self) -> Decimal:
        return sum(self.cells.values(), Decimal("0"))

    @property
    def insured_matches(self) -> bool:
        """База для взносов сходится с часами, которые в неё входят.

        Не косметика: по базе движок считает взносы и бруто, и расхождение
        означает расчёт по числу, которого в табеле нет.
        """
        return self.insured_hours == self.insured_declared


@dataclass
class Grid:
    columns: list[Column]
    rows: list[Row]

    @property
    def column_totals(self) -> dict[str, Decimal]:
        return {
            column.code: sum(
                (row.cells.get(column.code, Decimal("0")) for row in self.rows),
                Decimal("0"),
            )
            for column in self.columns
        }

    @property
    def total(self) -> Decimal:
        return sum((row.total for row in self.rows), Decimal("0"))


def build_columns(known: dict[str, dict]) -> list[Column]:
    return [
        Column(
            code=code,
            title=body.get("title") or code,
            pay_percent=(
                Decimal(str(body["pay_percent"])) if body.get("pay_percent") is not None
                else None
            ),
            # Пресет сам помечает то, что не подтверждено бухгалтером. Прятать
            # такие колонки нельзя (их могут заполнять), но и молчать о них —
            # значит выдать догадку за правило.
            unverified=body.get("status") == "unverified",
        )
        for code, body in known.items()
    ]


def visible_rows(tenant_id: UUID, period: date, unit_ids=None):
    """Табели периода, ограниченные точками пользователя.

    Пустой `unit_ids` — «все точки» (директор, бухгалтер). Непустой — только
    свои. Это не разграничение по точкам в базе (задача T022), а отказ строить
    редактируемый экран с заведомо открытой дырой.
    """
    rows = (
        Timesheet.objects.filter(tenant_id=tenant_id, period=period)
        .select_related("employee", "unit")
        .order_by("employee__last_name", "employee__first_name")
    )
    if unit_ids:
        rows = rows.filter(unit_id__in=list(unit_ids))
    return rows


def build_grid(tenant_id: UUID, period: date, *, unit_ids=None) -> Grid:
    known = hour_types(tenant_id, period, country_of(tenant_id))
    columns = build_columns(known)
    codes = [column.code for column in columns]

    # Закрытия читаются один раз на сетку, а не по строке на человека: 35 строк
    # дали бы 35 одинаковых запросов ради одного и того же ответа.
    closed = set(open_closures(tenant_id, period))

    rows = []
    for sheet in visible_rows(tenant_id, period, unit_ids):
        stored = sheet.hours or {}
        rows.append(
            Row(
                timesheet_id=sheet.id,
                employee=f"{sheet.employee.last_name} {sheet.employee.first_name}".strip(),
                external_id=sheet.employee.external_id,
                unit=sheet.unit.code if sheet.unit else "",
                norm_hours=sheet.norm_hours,
                insured_hours=sheet.insured_hours,
                insured_declared=insured_base(stored, known),
                # Типы, которых нет в правилах страны, на экран не попадают:
                # править то, чего движок не посчитает, нельзя. Если такие
                # часы в базе есть, их покажет отчёт импорта (T021).
                cells={code: Decimal(str(stored.get(code, 0))) for code in codes},
                unit_id=sheet.unit_id,
                closed=sheet.unit_id in closed,
            )
        )
    return Grid(columns=columns, rows=rows)
