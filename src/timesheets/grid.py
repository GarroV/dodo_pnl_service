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
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from core.models import Timesheet, Unit
from payroll import HOURS, insured_base, work_measure
from payrun.calc import terms_in_force
from payrun.rules import select_rules

from . import suspicion
from .authorship import Author, cell_authors, display_names
from .closing import open_closures
from .store import country_of

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
    # Пусто — строки табеля ещё нет: человек заведён с экрана, но часов ему
    # никто не вписывал (issue #152). Такая строка заводится первой правкой,
    # и до неё у неё нет ни id, ни дней, ни следа.
    timesheet_id: UUID | None
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
    # Кем заводить строку, когда её ещё нет: правка ячейки приходит с этим id
    # вместо id табеля.
    employee_id: UUID | None = None
    closed: bool = False
    # Чем меряется работа этого человека (D032, T075). Свойство строки, а не
    # сетки: на одном экране соседствуют почасовая кухня и сдельные курьеры —
    # в этом весь смысл поддержки обоих способов.
    measure: str = HOURS
    measure_title: str = ""
    piece_value: Decimal = Decimal("0")
    # Подозрительные числа строки (T118). Считаются здесь, а не в шаблоне:
    # правило общее с загрузкой файла (`timesheets/suspicion.py`), и разметка
    # его знать не должна.
    hints: list = field(default_factory=list)
    # Кто поставил часы каждого типа (T143, issue #52). Ключ — код типа часов,
    # то есть колонка сетки: вопрос задают про ячейку («кто поставил 176»), а не
    # про строку. Типа нет в словаре — автора не записано, и разметка говорит об
    # этом словами.
    authors: dict[str, Author] = field(default_factory=dict)
    # Кто последним правил величины самой строки — сдельную величину. У неё дня
    # нет, поэтому и след свой.
    edited_by_name: str = ""
    edited_at: datetime | None = None
    # Кто задал базу для взносов **руками** (T156). Отдельно от следа строки: до
    # этой задачи колонка базы показывала именно его, и правка соседних часов
    # приписывала базу тому, кто её не трогал. Пусто — её никто не задавал.
    insured_by_name: str = ""
    insured_at: datetime | None = None

    @property
    def suspicious(self) -> bool:
        return bool(self.hints)

    @property
    def hint_text(self) -> str:
        """Все подсказки строки одной фразой — для подписи и подсказки мыши."""
        return " · ".join(hint.text for hint in self.hints)

    # Сколько человек обязан отработать по договору и на сколько разошлось
    # (issue #171). Пусто — величины нет: про такого человека нельзя сказать
    # «недобрал», и в итогах точки он не участвует.
    contract_hours: Decimal | None = None

    # Оплачивается ли работа этой строки по часам. У почасовика да; у окладника
    # тоже — его часовая ставка выводится из оклада (issue #188), поэтому часы
    # ему оплачены и помечать их «не оплачиваются» нельзя. У сдельных нет.
    pays_by_hours: bool = True

    @property
    def contract_diff(self) -> Decimal | None:
        """На сколько отработанное разошлось с договором. Со знаком.

        Плюс — переработал, минус — недобрал. Знак важнее модуля: «12 часов
        расхождения» ничего не говорит управляющему, а «−12» говорит, что смену
        недоукомплектовали.
        """
        if self.contract_hours is None:
            return None
        return self.total - self.contract_hours

    @property
    def piecework(self) -> bool:
        return not self.pays_by_hours

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
    def has_piecework(self) -> bool:
        """Есть ли на экране хоть одна сдельная строка.

        От этого зависит, показывать ли колонку сдельной величины. Пустая
        колонка на каждом табеле мира была бы приглашением ввести в неё что-то
        там, где её никто не спрашивает.
        """
        return any(row.piecework for row in self.rows)

    @property
    def has_contract_hours(self) -> bool:
        """Есть ли хоть у кого договорные часы — от этого зависят две колонки.

        Пустые колонки «Договор» и «Δ» на табеле партнёра, который эту величину
        не ведёт, только отнимали бы ширину у часов.
        """
        return any(row.contract_hours is not None for row in self.rows)

    @property
    def contract_extra(self) -> Decimal:
        """Лишние часы по точке: сумма переработок, без взаимозачёта."""
        return sum(
            (row.contract_diff for row in self.rows
             if row.contract_diff is not None and row.contract_diff > 0),
            Decimal("0"),
        )

    @property
    def contract_missing(self) -> Decimal:
        """Недостающие часы: сумма недоработок, положительным числом.

        Отдельно от лишних, а не одной разностью: смена, где один переработал
        двадцать часов, а трое недобрали по семь, в сальдо выглядит ровной — и
        ровно это управляющему надо увидеть как две беды, а не как ноль.
        """
        return -sum(
            (row.contract_diff for row in self.rows
             if row.contract_diff is not None and row.contract_diff < 0),
            Decimal("0"),
        )

    @property
    def contract_balance(self) -> Decimal:
        """Сальдо: лишние минус недостающие. Люди без договора не участвуют."""
        return self.contract_extra - self.contract_missing

    @property
    def suspicious_rows(self) -> list:
        """Строки, о которых стоит сказать вслух, — в порядке самой сетки."""
        return [row for row in self.rows if row.suspicious]

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


def pays_by_hours(rules, term, measure: str) -> bool:
    """Оплачивается ли работа по часам: почасовая мера или оклад.

    Оклад отличается от сдельных мер тем, что часы у него оплачиваются —
    выведенной ставкой. Правило одно с движком (`work_measures.*.monthly`), а
    не список кодов здесь: заведёт партнёр вторую окладную меру — экран обязан
    понять её без правки.
    """
    if measure == HOURS:
        return True
    if term is None:
        return True
    preset = rules.preset(group_id=term.group_id, employee_id=term.employee_id)
    cfg = (preset.get("work_measures") or {}).get(measure) or {}
    # «Сумма целиком» часами не меряется: она не зависит от табеля вовсе.
    return bool(cfg.get("monthly")) and cfg.get("proration") != "none"


def measure_of(rules, term) -> tuple[str, str]:
    """Мера работы строки и её подпись — по правилам, действующим на этот месяц.

    Правила берутся ровно те же и тем же способом, каким их берёт расчёт
    (`payrun.calc`): с переопределениями группы и человека. Иначе экран
    предлагал бы вводить одно, а расчёт читал бы другое — и разошлись бы они
    молча, ровно у того партнёра, который правило и переопределил.

    Условий найма нет — меры нет: чем меряется работа, записано у группы, а
    группа человека живёт в условиях найма. Такую строку расчёт всё равно
    отвергнет по имени, и придумывать ей способ оплаты здесь не за чем.
    """
    if term is None:
        return HOURS, ""
    preset = rules.preset(group_id=term.group_id, employee_id=term.employee_id)
    # Мера человека сильнее правила группы (T164): сдельную величину обязан
    # спрашивать тот, у кого она своя, а не только вся его группа целиком.
    measure = work_measure(
        (preset.get("groups") or {}).get(term.group.code), employee=term.work_measure,
    )
    title = ((preset.get("work_measures") or {}).get(measure) or {}).get("title") or ""
    return measure, title


def build_grid(tenant_id: UUID, period: date, *, unit_ids=None) -> Grid:
    rules = select_rules(tenant_id, country_of(tenant_id), period)
    known = dict(rules.base.get("hour_types") or {})
    columns = build_columns(known)
    codes = [column.code for column in columns]

    # Закрытия читаются один раз на сетку, а не по строке на человека: 35 строк
    # дали бы 35 одинаковых запросов ради одного и того же ответа. То же и с
    # условиями найма: группа человека нужна каждой строке.
    closed = set(open_closures(tenant_id, period))
    terms = terms_in_force(tenant_id, period)

    # След правки читается двумя запросами на всю сетку, а не по запросу на
    # ячейку: ячеек 210, а разных ответов среди них два-три (T143).
    authors = cell_authors(tenant_id, period)
    sheets = list(visible_rows(tenant_id, period, unit_ids))
    # Имена спрашиваются одним запросом на обе колонки сразу: у следа строки и у
    # автора базы это чаще всего одни и те же несколько человек.
    row_editors = display_names(
        [sheet.edited_by for sheet in sheets] + [sheet.insured_by for sheet in sheets]
    )

    rows = []
    for sheet in sheets:
        stored = sheet.hours or {}
        measure, measure_title = measure_of(rules, terms.get(sheet.employee_id))
        name = f"{sheet.employee.last_name} {sheet.employee.first_name}".strip()
        rows.append(
            Row(
                timesheet_id=sheet.id,
                employee=name,
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
                measure=measure,
                measure_title=measure_title,
                pays_by_hours=pays_by_hours(rules, terms.get(sheet.employee_id), measure),
                contract_hours=_contract_of(terms.get(sheet.employee_id)),
                piece_value=Decimal(str(sheet.piece_value)),
                # Подсказки считаются по тем же числам, что показаны в строке, и
                # тем же правилом, каким их считает загрузка файла (T118).
                # Показанные типы часов при этом не фильтруются: тип, которого
                # нет в правилах страны, на экран не попадает, но отрицательные
                # часы в нём — та же опечатка, и молчать о ней нельзя.
                hints=suspicion.hints(
                    who=name,
                    hours=stored,
                    norm_hours=sheet.norm_hours,
                    dismissed_at=sheet.employee.dismissed_at,
                    period=period,
                    piecework=not pays_by_hours(rules, terms.get(sheet.employee_id), measure),
                ),
                authors={
                    code: authors[(sheet.id, code)]
                    for code in codes
                    if (sheet.id, code) in authors
                },
                edited_by_name=row_editors.get(sheet.edited_by, ""),
                edited_at=sheet.edited_at,
                insured_by_name=row_editors.get(sheet.insured_by, ""),
                insured_at=sheet.insured_at,
            )
        )

    # Люди, у которых условия найма действуют, а строки табеля ещё нет: заведён
    # с экрана посреди месяца (D049) — и до issue #152 он в табеле не
    # существовал, то есть часы ему вписать было некуда, а в ведомость он не
    # приходил вовсе. Строка показывается пустой и заводится первой правкой.
    rows.extend(
        _rows_without_a_sheet(
            tenant_id, period, terms=terms, codes=codes,
            seen={sheet.employee_id for sheet in sheets},
            unit_ids=unit_ids, closed=closed, rules=rules,
        )
    )
    # Порядок общий на всю сетку, а не «сначала заведённые, потом остальные»:
    # человек ищет фамилию, а не источник строки.
    rows.sort(key=lambda row: row.employee.casefold())
    return Grid(columns=columns, rows=rows)


def _rows_without_a_sheet(tenant_id, period, *, terms, codes, seen, unit_ids, closed, rules):
    """Пустые строки тех, кого ещё нет в табеле этого месяца."""
    from core.models import Employee

    waiting = {
        employee_id: term
        for employee_id, term in terms.items()
        if employee_id not in seen
        # Точки пользователя — то же правило, что у `visible_rows`: чужую точку
        # экран не показывает и через новый путь тоже.
        and (not unit_ids or term.unit_id in set(unit_ids))
    }
    if not waiting:
        return []

    norm = _norm_or_zero(tenant_id, period)
    people = Employee.objects.filter(tenant_id=tenant_id, id__in=list(waiting)).only(
        "id", "first_name", "last_name", "external_id", "dismissed_at",
    )
    units = {
        unit.id: unit.code
        for unit in Unit.objects.filter(tenant_id=tenant_id).only("id", "code")
    }
    empty = []
    for person in people:
        term = waiting[person.id]
        measure, measure_title = measure_of(rules, term)
        name = f"{person.last_name} {person.first_name}".strip()
        empty.append(
            Row(
                timesheet_id=None,
                employee_id=person.id,
                employee=name,
                external_id=person.external_id,
                unit=units.get(term.unit_id, ""),
                norm_hours=norm,
                insured_hours=Decimal("0"),
                insured_declared=Decimal("0"),
                cells={code: Decimal("0") for code in codes},
                unit_id=term.unit_id,
                closed=term.unit_id in closed,
                measure=measure,
                measure_title=measure_title,
                pays_by_hours=pays_by_hours(rules, term, measure),
                contract_hours=_contract_of(term),
            )
        )
    return empty


def _contract_of(term) -> Decimal | None:
    """Договорные часы человека этого месяца. Нет условий — нет и величины."""
    if term is None or term.contract_hours is None:
        return None
    return Decimal(str(term.contract_hours))


def _norm_or_zero(tenant_id, period) -> Decimal:
    """Норма месяца для показа. Нет календаря — ноль, а не отказ строить экран.

    Отказ здесь означал бы, что один незаведённый месяц календаря прячет весь
    табель. Заведение строки норму всё равно спросит и без календаря откажет
    словами (`store.ensure_row`) — то есть человек узнает причину в тот момент,
    когда она мешает, а не при открытии страницы.
    """
    from .store import CellRefused, month_norm

    try:
        return month_norm(tenant_id, period)
    except CellRefused:
        return Decimal("0")
