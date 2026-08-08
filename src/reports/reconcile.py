"""Сверка расчёта продукта с таблицей бухгалтера (T031).

Не второй импортёр: файл разбирает `payroll.importers.plata_xlsx`, тот же, что
уже читает таблицу партнёра в табель. Здесь — **сравнение** и только оно.

Три решения, на которых всё держится.

**Итоги берутся из `payslip_totals` — и только оттуда.** Нето, бруто, взносы и
полная стоимость посчитаны по строке ведомости целиком, поэтому база отдаёт их
лишь роли, которой видны все регистры вообще (T071). Строку, итогов которой она
не отдала, сравнивать по деньгам не с чем — и **ни одного нашего числа** по ней
не показывается. Подставить туда сумму видимых компонентов значило бы выдать
скрытый регистр вычитанием: разница с итогом файла и есть скрытая часть. Ровно
так устроены две уже закрытые в этом продукте утечки (T050, T071).

**Но входы сверяются всегда.** Часы, ставка и коэффициент к регистрам учёта
отношения не имеют, и роль видит их по обычным политикам. Поэтому строка без
доступных итогов не выбрасывается: по ней сверяются входы, а деньги честно
остаются пустыми. Это и есть польза сверки для роли с ограниченным доступом —
расхождение чаще всего сидит именно во входах.

**Совпадение — до копейки.** Допуска в сравнении нет: зашитый допуск однажды
спрячет настоящее расхождение ровно того же размера. Расхождение меньше динара
называется округлением (`ROUNDING`) и показывается отдельной группой — известное
расхождение с бухгалтерией, которое видно, но не мешает читать существенное.

**Расхождение обязано объяснить себя входами.** «Не сошлось на 812» не говорит
бухгалтеру ничего. Поэтому у каждой разошедшейся строки перечислено, что
разошлось на входе — часы по видам, застрахованные часы, ставка, коэффициент,
надбавка, проставленная руками. Если входы сошлись, а итог нет, значит разошлось
**правило**, и это тоже ответ.

Оформления здесь нет намеренно, как и в `reports.sheet`: подписи и формат чисел —
дело того, кто показывает.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

from payroll.importers import Finding
from payroll.importers.plata_xlsx import ImportedRow

# Поля, которые бухгалтерия считает сама и пишет в свою таблицу. Те же четыре,
# что сверяет регрессия движка (`tests/_payroll_checks.FIELDS`): разъехавшиеся
# списки означали бы, что продукт и тесты сверяют разное.
FIELDS = ("net", "gross", "contributions", "total_cost")

# Ниже этого расхождение считается округлением на стороне бухгалтерии. Это НЕ
# допуск сравнения: строка с копейками не сходится, она лишь названа иначе.
ROUNDING = Decimal("1.00")

# Код надбавки, которую бухгалтер иногда проставляет руками вместо правила
# (колонка «TOPLI OBROK I REGRES»). Названа причиной, а не подогнана: по какому
# основанию она делится — по дням или по часам — вопрос владельца (Q003), и
# сверка его не решает, а показывает.
MEAL_CODE = "meal_and_vacation_bonus"


@dataclass(frozen=True)
class Amount:
    """Одно сравниваемое число: что в файле, что в расчёте."""

    code: str
    expected: Decimal | None   # таблица бухгалтера
    actual: Decimal | None     # расчёт продукта

    @property
    def comparable(self) -> bool:
        """Сравнивать можно, только когда есть обе стороны.

        Не прочитанное из файла поле — не ноль: принять его за ноль значит
        объявить расхождение на всю сумму там, где сравнивать нечего.
        """
        return self.expected is not None and self.actual is not None

    @property
    def diff(self) -> Decimal | None:
        return self.actual - self.expected if self.comparable else None

    @property
    def matches(self) -> bool:
        return self.comparable and self.diff == 0

    @property
    def rounding(self) -> bool:
        return self.comparable and self.diff != 0 and abs(self.diff) < ROUNDING


@dataclass(frozen=True)
class Cause:
    """Что разошлось на входе. Не расхождение само по себе — его объяснение."""

    kind: str          # hours | insured | rate | coefficient | meal
    code: str          # вид часов для kind == "hours", иначе пусто
    expected: Decimal
    actual: Decimal


@dataclass(frozen=True)
class Line:
    """Строка сверки: человек, у которого есть обе стороны."""

    key: str
    name: str
    sheet: str
    amounts: list[Amount]
    causes: list[Cause] = field(default_factory=list)

    @property
    def compared(self) -> bool:
        """Сравнивались ли деньги вообще.

        Строка без единого сравнимого числа — это не «сошлось»: сравнивать было
        нечего. Пустое `all()` даёт истину, и без этого различения сверка
        отрапортовала бы совпадение там, где не сверила ничего.
        """
        return any(a.comparable for a in self.amounts)

    @property
    def matched(self) -> bool:
        """Сошлось всё, что можно было сравнить."""
        return self.compared and all(a.matches for a in self.amounts if a.comparable)

    @property
    def rounding_only(self) -> bool:
        """Разошлось только на копейки — известное расхождение округления."""
        return self.compared and not self.matched and all(
            a.matches or a.rounding for a in self.amounts if a.comparable
        )

    @property
    def worst(self) -> Decimal:
        return max(
            (abs(a.diff) for a in self.amounts if a.diff is not None),
            default=Decimal(0),
        )


@dataclass(frozen=True)
class Absent:
    """Строка, у которой есть только одна сторона."""

    key: str
    name: str
    sheet: str
    why: str


@dataclass
class Reconciliation:
    """Ответ сверки целиком — то, что показывается человеку."""

    lines: list[Line] = field(default_factory=list)
    only_in_file: list[Absent] = field(default_factory=list)
    only_in_run: list[Absent] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return sum(1 for line in self.lines if line.matched)

    @property
    def rounding(self) -> int:
        return sum(1 for line in self.lines if line.rounding_only)

    @property
    def mismatched(self) -> int:
        return sum(
            1 for line in self.lines
            if line.compared and not line.matched and not line.rounding_only
        )

    @property
    def inputs_only(self) -> int:
        """Строки, у которых сверены только входы: денег роли не выдано."""
        return sum(1 for line in self.lines if not line.compared)

    @property
    def clean(self) -> bool:
        """Сошлось всё и целиком: ни расхождений, ни потерянных строк, ни находок.

        Строка, у которой сверены только входы, чистой сверку не делает: деньги
        по ней не сравнивались, и объявлять это совпадением нельзя.
        """
        return not (
            self.mismatched or self.rounding or self.inputs_only
            or self.only_in_file or self.only_in_run or self.findings
        )

    def _sum(self, side: str) -> Decimal:
        """Итог по нето — только по сопоставленным строкам.

        Несопоставленное в итог не течёт намеренно: сумма, в которой смешаны
        сверенные и несверенные строки, отвечает не на тот вопрос, ради
        которого сверку открыли.
        """
        return sum(
            (
                getattr(a, side)
                for line in self.lines
                for a in line.amounts
                if a.code == "net" and a.comparable
            ),
            Decimal(0),
        )

    @property
    def total_expected(self) -> Decimal:
        return self._sum("expected")

    @property
    def total_actual(self) -> Decimal:
        return self._sum("actual")

    @property
    def total_diff(self) -> Decimal:
        return self.total_actual - self.total_expected


@dataclass(frozen=True)
class RunLine:
    """Наша сторона: строка расчёта, которую база отдала этой роли."""

    key: str
    name: str
    totals: dict[str, Decimal]
    hours: dict[str, Decimal]
    insured_hours: Decimal | None = None
    base_rate: Decimal | None = None
    coefficient: Decimal | None = None
    meal: Decimal | None = None


def _num(value) -> Decimal:
    return Decimal(str(value or 0))


def _hour_causes(theirs: dict, ours: dict) -> list[Cause]:
    """Расхождение часов по видам. Отсутствующий вид — это ноль, а не пропуск."""
    causes = []
    for kind in sorted(set(theirs) | set(ours)):
        want, got = _num(theirs.get(kind)), _num(ours.get(kind))
        if want != got:
            causes.append(Cause("hours", kind, want, got))
    return causes


def _causes(row: ImportedRow, run: RunLine) -> list[Cause]:
    """Чем объяснить расхождение. Только то, что действительно разошлось."""
    causes = _hour_causes(row.timesheet.hours, run.hours)

    pairs = (
        ("insured", _num(row.timesheet.insured_hours), run.insured_hours),
        ("rate", _num(row.employee.base_rate), run.base_rate),
        ("coefficient", _num(row.employee.coefficient), run.coefficient),
        ("meal", row.sheet_meal, run.meal),
    )
    for kind, want, got in pairs:
        # Пусто с любой стороны — не расхождение, а отсутствие данных для
        # сравнения. Надбавки в файле может не быть вовсе, и это обычное дело.
        if want is None or got is None:
            continue
        if _num(want) != _num(got):
            causes.append(Cause(kind, "", _num(want), _num(got)))
    return causes


def compare(
    rows: list[ImportedRow],
    run: dict[str, RunLine],
    *,
    findings: list[Finding] | None = None,
) -> Reconciliation:
    """Сравнить строки файла с тем, что база отдала этой роли. Чистая функция."""
    result = Reconciliation(findings=list(findings or []))
    seen: set[str] = set()

    for row in rows:
        key = row.employee.ext_id
        seen.add(key)
        ours = run.get(key)
        if ours is None:
            # Ни числа, ни разницы. «Нет в расчёте» — это ответ базы, и он
            # одинаков для того, кого в расчёте действительно нет, и для того,
            # чью строку роли не видно. Различить их снаружи нельзя намеренно.
            result.only_in_file.append(Absent(
                key, row.name, row.sheet, "в расчёте этого периода такой строки нет",
            ))
            continue

        line = Line(
            key=key, name=row.name, sheet=row.sheet,
            amounts=[
                Amount(code, row.expected.get(code), ours.totals.get(code))
                for code in FIELDS
            ],
        )
        # Причины — объяснение расхождения. У сошедшейся строки объяснять
        # нечего, и перечислять там разошедшиеся входы значило бы звать
        # человека разбираться там, где всё в порядке. А вот у строки, деньги
        # которой не сравнивались, входы — единственное, что сверка вообще
        # может сказать, и они показываются всегда.
        if not line.matched:
            line = Line(
                key=line.key, name=line.name, sheet=line.sheet,
                amounts=line.amounts, causes=_causes(row, ours),
            )
        result.lines.append(line)

    for key, ours in run.items():
        if key not in seen:
            result.only_in_run.append(Absent(
                key, ours.name, "", "в загруженной таблице такой строки нет",
            ))

    # Сверху то, что разошлось сильнее: сверку читают сверху вниз и до первой
    # понятой причины.
    result.lines.sort(key=lambda line: (-line.worst, line.name))
    result.only_in_file.sort(key=lambda item: item.name)
    result.only_in_run.sort(key=lambda item: item.name)
    return result


# --- наша сторона из базы -----------------------------------------------------


def collect_run(tenant_id: UUID, period: date) -> dict[str, RunLine]:
    """Что база отдала **этой роли** по периоду, по ключу сотрудника.

    Две разные выборки с разной видимостью, и их нельзя смешивать:

    * **входы** — табель и условия найма — видны по обычным политикам; регистр
      учёта к ним отношения не имеет;
    * **итоги** — `payslip_totals` — видны только роли, которой доступны все
      регистры вообще (T071), потому что посчитаны по строке ведомости целиком.

    Поэтому строка появляется всегда, когда виден её вход, а деньги в ней
    заполняются, только если база их отдала. Досчитывать итог из видимых
    компонентов нельзя: разница с файлом и была бы скрытой частью.
    """
    # Модели импортируются здесь: всё выше — чистые функции, и настройки Django
    # ради них подниматься не должны.
    from core.models import Employee, EmploymentTerm, PayComponent, PayslipTotals, Timesheet

    sheets = {
        row.employee_id: row
        for row in Timesheet.objects.filter(tenant_id=tenant_id, period=period)
    }
    totals = {
        row.payslip.employee_id: row
        for row in PayslipTotals.objects.filter(
            tenant_id=tenant_id, payslip__payrun__period=period
        ).select_related("payslip")
    }

    known = set(sheets) | set(totals)
    if not known:
        return {}

    # Надбавка сверяется только там, где отданы итоги: у строки без них
    # видимая часть компонентов неполна, и «наша» надбавка означала бы не то,
    # что в таблице бухгалтера.
    meals: dict[UUID, Decimal] = {}
    for component in PayComponent.objects.filter(
        tenant_id=tenant_id,
        payslip_id__in=[row.payslip_id for row in totals.values()],
        code=MEAL_CODE,
    ):
        meals[component.payslip_id] = (
            meals.get(component.payslip_id, Decimal(0)) + component.amount
        )

    terms = {
        term.employee_id: term
        for term in EmploymentTerm.objects.filter(
            tenant_id=tenant_id, employee_id__in=list(known), valid_from__lte=period,
        ).order_by("valid_from")
    }
    people = {
        person.id: person
        for person in Employee.objects.filter(tenant_id=tenant_id, id__in=list(known))
    }

    out: dict[str, RunLine] = {}
    for employee_id in known:
        person = people.get(employee_id)
        if person is None:
            # Человек не виден роли — значит и строки о нём быть не должно.
            continue
        sheet, row, term = sheets.get(employee_id), totals.get(employee_id), terms.get(employee_id)
        out[person.external_id] = RunLine(
            key=person.external_id,
            name=f"{person.last_name} {person.first_name}".strip(),
            totals={code: getattr(row, code) for code in FIELDS} if row else {},
            hours={k: _num(v) for k, v in (sheet.hours if sheet else {}).items()},
            insured_hours=sheet.insured_hours if sheet else None,
            base_rate=term.base_rate if term else None,
            coefficient=term.coefficient if term else None,
            meal=meals.get(row.payslip_id) if row else None,
        )
    return out


def reconcile(file, *, tenant_id: UUID, period: date) -> Reconciliation:
    """Сверить загруженный файл с расчётом периода.

    Файл нигде не сохраняется — ни на диск, ни в базу. Это не экономия, а D028:
    в таблице партнёра ФИО и суммы живых людей, и сверка не повод заводить им
    ещё одно место жительства. Она разовая операция, её ответ живёт на экране.
    """
    from payroll.importers import read_plata_file

    parsed = read_plata_file(file)
    return compare(
        parsed.rows, collect_run(tenant_id, period), findings=list(parsed.findings)
    )
