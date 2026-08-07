"""Ведомость: строка — сотрудник, колонки — компоненты выплаты.

Два решения, на которых всё держится.

**Ведомость собирается только из `pay_components`** — не из `payslips`.
Ограничивающая политика видимости регистров стоит на компонентах; суммарные
поля ведомости (`net`, `gross`, `contributions`) посчитаны по всем регистрам
сразу и такой политики не имеют. Покажи мы их — бухгалтер, которому виден
только официальный регистр, получил бы скрытые от него суммы вычитанием. Итог
«по видимому срезу» из D023 получается тогда сам собой: складывается ровно то,
что человек видит на экране.

**Строка — пара «сотрудник × регистр».** Регистр — свойство компонента, а не
человека: у сотрудника кухни часы идут в дополнительном регистре, а надбавка за
питание — в официальном. Поэтому одна ведомость с меткой регистра (D023), а не
три отдельные и не подкраска строк.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

# Порядок регистров на экране — от самого «внешнего» к внутреннему, всегда
# одинаковый: перескакивающие местами строки читаются как разные данные.
LEDGER_ORDER = ["white", "grey", "black"]


@dataclass(frozen=True)
class Cell:
    """Одна сумма: чья, где, в каком регистре и по какому компоненту."""

    employee: str
    unit: str
    ledger: str
    code: str
    title: str
    amount: Decimal
    key: str = ""  # чем различать однофамильцев; по умолчанию — имя

    @property
    def employee_key(self) -> str:
        return self.key or self.employee


@dataclass(frozen=True)
class Column:
    code: str
    title: str


@dataclass(frozen=True)
class Row:
    employee: str
    unit: str
    ledger: str
    amounts: dict[str, Decimal]
    total: Decimal


@dataclass(frozen=True)
class Sheet:
    columns: list[Column]
    rows: list[Row]
    ledger_totals: list[tuple[str, Decimal]]
    column_totals: dict[str, Decimal]
    total: Decimal
    employees: int

    def __bool__(self) -> bool:
        return bool(self.rows)


def _column_key(code: str) -> tuple[int, str]:
    """Часы впереди: с них начинается любая проверка ведомости глазами."""
    return (0 if code.startswith("hours.") else 1, code)


def _ledger_key(ledger: str) -> tuple[int, str]:
    return (LEDGER_ORDER.index(ledger) if ledger in LEDGER_ORDER else len(LEDGER_ORDER), ledger)


def assemble(cells: list[Cell]) -> Sheet:
    """Собрать ведомость из видимых сумм. Всё, что не видно, сюда не приезжает."""
    titles: dict[str, str] = {}
    grouped: dict[tuple[str, str], dict] = {}

    for cell in cells:
        titles.setdefault(cell.code, cell.title)
        row = grouped.setdefault(
            (cell.employee_key, cell.ledger),
            {"employee": cell.employee, "unit": cell.unit, "amounts": {}},
        )
        amounts = row["amounts"]
        amounts[cell.code] = amounts.get(cell.code, Decimal(0)) + cell.amount

    columns = [Column(code, titles[code]) for code in sorted(titles, key=_column_key)]

    rows = [
        Row(
            employee=body["employee"], unit=body["unit"], ledger=ledger,
            amounts=body["amounts"], total=sum(body["amounts"].values(), Decimal(0)),
        )
        for (_, ledger), body in sorted(
            grouped.items(), key=lambda item: (item[1]["employee"], _ledger_key(item[0][1]))
        )
    ]

    ledger_totals: dict[str, Decimal] = {}
    column_totals: dict[str, Decimal] = {}
    for row in rows:
        ledger_totals[row.ledger] = ledger_totals.get(row.ledger, Decimal(0)) + row.total
        for code, amount in row.amounts.items():
            column_totals[code] = column_totals.get(code, Decimal(0)) + amount

    return Sheet(
        columns=columns,
        rows=rows,
        ledger_totals=sorted(ledger_totals.items(), key=lambda item: _ledger_key(item[0])),
        column_totals=column_totals,
        # Итог — сумма показанных строк, а не отдельная выборка: иначе он мог бы
        # разойтись с тем, что человек видит, и это никто бы не заметил.
        total=sum((row.total for row in rows), Decimal(0)),
        employees=len({row.employee for row in rows}),
    )


def build_sheet(tenant_id: UUID, period: date) -> Sheet:
    """Ведомость периода из базы. Фильтра по регистру нет — его ставит база."""
    # Модели импортируются здесь: всё выше — чистые функции, и настройки Django
    # ради них подниматься не должны.
    from core.models import PayComponent

    cells = [
        Cell(
            employee=f"{component.payslip.employee.last_name} "
                     f"{component.payslip.employee.first_name}".strip(),
            unit=component.payslip.unit.code if component.payslip.unit_id else "",
            ledger=component.layer,
            code=component.code,
            title=component.title,
            amount=component.amount,
            key=component.payslip.employee.external_id,
        )
        for component in PayComponent.objects.filter(
            tenant_id=tenant_id, payslip__payrun__period=period
        ).select_related("payslip__employee", "payslip__unit")
    ]
    return assemble(cells)
