"""Ведомость: строка — сотрудник, колонки — компоненты выплаты.

Два решения, на которых всё держится.

**Ведомость собирается только из `pay_components`** — не из итогов расчёта.
Ограничивающая политика видимости регистров стоит на компонентах, и итог «по
видимому срезу» из D023 получается сам собой: складывается ровно то, что человек
видит на экране. Суммарные поля (`net`, `gross`, `contributions`) посчитаны по
всем регистрам сразу и живут в отдельной таблице `payslip_totals`, закрытой
своей политикой (T050): роль видит их, только если видит все регистры строки.

**Разница за закрытый месяц — отдельная строка** (T026). Ключ строки включает
месяц-источник, иначе перенос по `hours.regular` слился бы с обычной колонкой
того же кода и стал бы невидимым: бухгалтер увидел бы поехавшую сумму без следа
причины. Суммы при этом стоят в своих обычных колонках — видно не только «есть
разница», но и какой компонент изменился.

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
    # Пусто — обычная сумма этого месяца. Заполнено — разница за указанный
    # закрытый месяц, перенесённая сюда (T026).
    retro_source: date | None = None

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
    # Ключ сотрудника (`employees.external_id`) — тот же, которым строки
    # группировались. Хранится рядом с отображаемым именем намеренно: выгрузка
    # ищет по нему статью P&L, и пока ключа здесь не было, она спрашивала
    # справочник ОТОБРАЖАЕМЫМ именем. Совпасть они не могут («ФАМИЛИЯ ИМЯ»
    # против ключа), поэтому у всех начислений в файле стояло «Без статьи», а
    # файл выглядел нормальным (issue #95).
    ledger: str
    amounts: dict[str, Decimal]
    total: Decimal
    employee_key: str = ""
    payslip_id: UUID | None = None
    frozen: bool = False
    freeze_reason: str = ""
    retro_source: date | None = None

    @property
    def is_retro(self) -> bool:
        return self.retro_source is not None


@dataclass(frozen=True)
class ColumnGroup:
    """Ярус групп в шапке ведомости (issue #163, модуль 9 эталона).

    Эталон рисует над колонками группы — «Начисления», «Удержания», — и это не
    украшение: в ведомости полтора десятка колонок, и без иерархии человек ищет
    нужную пересчётом слева направо.

    Наши группы другие, чем в макете, и это осознанно. Удержаний в расчёте нет
    ни одного компонента: налог и взносы производные и колонками не приходят.
    Пустая группа «Удержания» обещала бы колонки, которых нет. Поэтому
    группируется то, что есть: оплата часов отдельно от надбавок и
    корректировок; третья группа появится вместе с первым удержанием.
    """

    # Код, а не готовое название: `payrun/sheet.py` — чистый Python без Django,
    # и каталог переводов ему недоступен. Слова для человека живут в вебе
    # (`web/views.py`), и там же они извлекаются в каталог — переводить строку
    # из переменной `makemessages` не умеет, что и вскрылось на первом прогоне.
    code: str
    span: int


@dataclass(frozen=True)
class Sheet:
    columns: list[Column]
    groups: list[ColumnGroup]
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


# Коды групп — здесь: что чем считается, решает устройство ведомости, а не
# разметка. Слова для человека — в вебе.
HOURS_GROUP = "hours"
OTHER_GROUP = "other"


def _groups_of(columns: list[Column]) -> list[ColumnGroup]:
    """Ярус групп над колонками. Пустых групп не бывает.

    Границы совпадают с порядком колонок (`_column_key`): часы идут первыми,
    поэтому группа — это отрезок, а не набор вразнобой. Если однажды порядок
    поменяется, группы поедут вместе с ним, а не разъедутся молча: и то и
    другое считается одной функцией сортировки.
    """
    groups: list[ColumnGroup] = []
    for column in columns:
        code = HOURS_GROUP if column.code.startswith("hours.") else OTHER_GROUP
        if groups and groups[-1].code == code:
            groups[-1] = ColumnGroup(code, groups[-1].span + 1)
        else:
            groups.append(ColumnGroup(code, 1))
    return groups


def _ledger_key(ledger: str) -> tuple[int, str]:
    return (LEDGER_ORDER.index(ledger) if ledger in LEDGER_ORDER else len(LEDGER_ORDER), ledger)


def assemble(cells: list[Cell]) -> Sheet:
    """Собрать ведомость из видимых сумм. Всё, что не видно, сюда не приезжает."""
    titles: dict[str, str] = {}
    grouped: dict[tuple[str, str], dict] = {}

    for cell in cells:
        titles.setdefault(cell.code, cell.title)
        row = grouped.setdefault(
            (cell.employee_key, cell.ledger, cell.retro_source),
            {
                "employee": cell.employee, "employee_key": cell.employee_key,
                "unit": cell.unit, "amounts": {},
                "payslip_id": cell.payslip_id, "frozen": cell.frozen,
                "freeze_reason": cell.freeze_reason, "retro_source": cell.retro_source,
            },
        )
        amounts = row["amounts"]
        amounts[cell.code] = amounts.get(cell.code, Decimal(0)) + cell.amount

    columns = [Column(code, titles[code]) for code in sorted(titles, key=_column_key)]
    groups = _groups_of(columns)

    rows = [
        Row(
            employee=body["employee"], employee_key=body["employee_key"],
            unit=body["unit"], ledger=ledger,
            amounts=body["amounts"], total=sum(body["amounts"].values(), Decimal(0)),
            payslip_id=body["payslip_id"], frozen=body["frozen"],
            freeze_reason=body["freeze_reason"], retro_source=body["retro_source"],
        )
        # Перенос идёт сразу после своей обычной строки, а не в конце ведомости:
        # разницу читают рядом с тем, к чему она относится. Пустой источник
        # сортируется первым — обычная строка впереди своей дельты.
        for (_, ledger, _source), body in sorted(
            grouped.items(),
            key=lambda item: (
                item[1]["employee"],
                _ledger_key(item[0][1]),
                item[0][2] or date.min,
            ),
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
        groups=groups,
        rows=rows,
        ledger_totals=sorted(ledger_totals.items(), key=lambda item: _ledger_key(item[0])),
        column_totals=column_totals,
        # Итог — сумма показанных строк, а не отдельная выборка: иначе он мог бы
        # разойтись с тем, что человек видит, и это никто бы не заметил.
        total=sum((row.total for row in rows), Decimal(0)),
        employees=len({row.employee for row in rows}),
    )


def collect_cells(tenant_id: UUID, period: date) -> list[Cell]:
    """Видимые суммы периода из базы. Фильтра по регистру нет — его ставит база.

    Отделено от `build_sheet` затем, что ведомость показывается не только
    целиком: разрез по регистру и выгрузка (блок `reports`) обязаны собираться
    **из тех же самых сумм**, а не второй выборкой рядом. Две выборки одного и
    того же расходятся молча — и тогда выгрузка отдаёт не то, что человек видел
    на экране.
    """
    # Модели импортируются здесь: всё выше — чистые функции, и настройки Django
    # ради них подниматься не должны.
    from core.models import PayComponent

    from .freezing import active_freezes

    # Заморозки видны по тем же политикам, что и сами строки ведомости:
    # приложение выборку не сужает (D014).
    freezes = active_freezes(tenant_id, period)

    return [
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
            retro_source=component.retro_source_period,
        )
        for component in PayComponent.objects.filter(
            tenant_id=tenant_id, payslip__payrun__period=period
        ).select_related("payslip__employee", "payslip__unit")
    ]


def build_sheet(tenant_id: UUID, period: date) -> Sheet:
    """Ведомость периода целиком — всё, что видно этой роли."""
    return assemble(collect_cells(tenant_id, period))
