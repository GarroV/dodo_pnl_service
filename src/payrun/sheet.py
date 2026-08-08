"""Ведомость: строка — сотрудник, колонки — компоненты выплаты.

Два решения, на которых всё держится.

**Ведомость собирается только из `pay_components`** — не из итогов расчёта.
Ограничивающая политика видимости регистров стоит на компонентах, и итог «по
видимому срезу» из D023 получается сам собой: складывается ровно то, что человек
видит на экране. Суммарные поля (`net`, `gross`, `contributions`) посчитаны по
всем регистрам сразу и живут в отдельной таблице `payslip_totals`, закрытой
своей политикой (T050): роль видит их, только если видит все регистры строки.

**Замороженная строка помечена** (T027): по человеку идёт спор, его числа
пересчёт не трогает. Метка едет вместе со строкой, а не спрашивается отдельно
экраном, — иначе ведомость и пометка разъезжались бы при любой фильтрации.

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
LEDGER_ORDER = ["official", "supplementary", "internal"]


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
    # Строка ведомости, к которой относится сумма, и её заморозка (T027).
    # Заморозка у сотрудника одна на все его регистры: морозится строка
    # ведомости целиком, а не отдельная её половина.
    payslip_id: UUID | None = None
    frozen: bool = False
    freeze_reason: str = ""

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
    payslip_id: UUID | None = None
    frozen: bool = False
    freeze_reason: str = ""


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
    """Часы впереди, отработанные — первыми: так ведомость читают глазами.

    Внутри групп порядок по коду. Он произволен, но одинаков от периода к
    периоду, а это здесь важнее: колонки, меняющиеся местами, читаются как
    другие данные.
    """
    if code == "hours.regular":
        return (0, "")
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
            {
                "employee": cell.employee, "unit": cell.unit, "amounts": {},
                "payslip_id": cell.payslip_id, "frozen": cell.frozen,
                "freeze_reason": cell.freeze_reason,
            },
        )
        amounts = row["amounts"]
        amounts[cell.code] = amounts.get(cell.code, Decimal(0)) + cell.amount

    columns = [Column(code, titles[code]) for code in sorted(titles, key=_column_key)]

    rows = [
        Row(
            employee=body["employee"], unit=body["unit"], ledger=ledger,
            amounts=body["amounts"], total=sum(body["amounts"].values(), Decimal(0)),
            payslip_id=body["payslip_id"], frozen=body["frozen"],
            freeze_reason=body["freeze_reason"],
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

    from .freezing import active_freezes

    # Заморозки видны по тем же политикам, что и сами строки ведомости:
    # приложение выборку не сужает (D014).
    freezes = active_freezes(tenant_id, period)

    cells = [
        Cell(
            employee=f"{component.payslip.employee.last_name} "
                     f"{component.payslip.employee.first_name}".strip(),
            unit=component.payslip.unit.code if component.payslip.unit_id else "",
            ledger=component.ledger,
            code=component.code,
            title=component.title,
            amount=component.amount,
            key=component.payslip.employee.external_id,
            payslip_id=component.payslip_id,
            frozen=component.payslip_id in freezes,
            freeze_reason=(
                freezes[component.payslip_id].reason
                if component.payslip_id in freezes else ""
            ),
        )
        for component in PayComponent.objects.filter(
            tenant_id=tenant_id, payslip__payrun__period=period
        ).select_related("payslip__employee", "payslip__unit")
    ]
    return assemble(cells)
