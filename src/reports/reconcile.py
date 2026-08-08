"""Сверка расчёта продукта с таблицей бухгалтера (T031).

Не второй импортёр: файл разбирает `payroll.importers.plata_xlsx`, тот же, что
уже читает таблицу партнёра в табель. Здесь — **сравнение** и только оно.

Три решения, на которых всё держится.

**Наша сторона берётся из `payslip_totals`.** Эта таблица по построению видна
роли, только если ей видны все регистры строки (T050): нето, бруто и взносы
посчитаны по всем регистрам сразу и в разрезе одного регистра не существуют.
Значит, строку, итогов которой база не отдала, сверять не с чем — и она уходит
в «нет в расчёте» **без единого нашего числа**. Показать по ней сумму видимых
компонентов и разницу с файлом значило бы выдать скрытый регистр вычитанием:
ровно так устроены две уже закрытые в этом продукте утечки (T050, T071).
Приложение при этом ничего не маскирует и ничего не досчитывает — границу
целиком держит база, как и требует D014.

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
    def matched(self) -> bool:
        """Сошлось всё, что можно было сравнить."""
        return all(a.matches for a in self.amounts if a.comparable)

    @property
    def rounding_only(self) -> bool:
        """Разошлось только на копейки — известное расхождение округления."""
        return not self.matched and all(
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
            1 for line in self.lines if not line.matched and not line.rounding_only
        )

    @property
    def clean(self) -> bool:
        """Сошлось всё и целиком: ни расхождений, ни потерянных строк, ни находок."""
        return not (
            self.mismatched or self.rounding or self.only_in_file
            or self.only_in_run or self.findings
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
        # человека разбираться там, где всё в порядке.
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
    """Строки расчёта, которые база отдала **этой роли**, по ключу сотрудника.

    Выборка идёт от `payslip_totals`, а не от `payslips`: строка ведомости
    видна и тому, кому доступна лишь часть её регистров, а итоги — только тому,
    кому видна вся строка (T050). Именно эта граница и нужна сверке.
    """
    # Модели импортируются здесь: всё выше — чистые функции, и настройки Django
    # ради них подниматься не должны.
    from core.models import EmploymentTerm, PayComponent, PayslipTotals, Timesheet

    totals = {
        row.payslip_id: row
        for row in PayslipTotals.objects.filter(
            tenant_id=tenant_id, payslip__payrun__period=period
        ).select_related("payslip__employee")
    }
    if not totals:
        return {}

    # Надбавка — из компонентов той же строки. Отдельной выборкой её брать
    # нельзя: у строки, итогов которой роли не видно, компоненты частично
    # видны, и надбавка «появилась бы» у человека, которого в сверке нет.
    meals: dict[UUID, Decimal] = {}
    for component in PayComponent.objects.filter(
        tenant_id=tenant_id, payslip_id__in=list(totals), code=MEAL_CODE
    ):
        meals[component.payslip_id] = (
            meals.get(component.payslip_id, Decimal(0)) + component.amount
        )

    employees = {row.payslip.employee_id: row.payslip_id for row in totals.values()}
    sheets = {
        row.employee_id: row
        for row in Timesheet.objects.filter(
            tenant_id=tenant_id, period=period, employee_id__in=list(employees)
        )
    }
    terms = {
        term.employee_id: term
        for term in EmploymentTerm.objects.filter(
            tenant_id=tenant_id, employee_id__in=list(employees),
            valid_from__lte=period,
        ).order_by("valid_from")
    }

    out: dict[str, RunLine] = {}
    for payslip_id, row in totals.items():
        person = row.payslip.employee
        sheet = sheets.get(person.id)
        term = terms.get(person.id)
        out[person.external_id] = RunLine(
            key=person.external_id,
            name=f"{person.last_name} {person.first_name}".strip(),
            totals={code: getattr(row, code) for code in FIELDS},
            hours={k: _num(v) for k, v in (sheet.hours if sheet else {}).items()},
            insured_hours=sheet.insured_hours if sheet else None,
            base_rate=term.base_rate if term else None,
            coefficient=term.coefficient if term else None,
            meal=meals.get(payslip_id),
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
