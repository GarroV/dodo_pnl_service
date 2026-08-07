"""Расчёт периода: из данных тенанта в ведомость.

Порядок и его причины:

1. **Собрать вход** — табели за месяц плюс условия найма, действующие в этом
   месяце. Сотрудник с табелем, но без условий найма — не «пропустим молча», а
   отказ с именем: посчитанный без него месяц выглядел бы правильным.
2. **Посчитать** движком `payroll` на пресете страны. Ничего странового здесь
   нет и быть не должно.
3. **Проверить регистры до записи.** Расчёт раскладывается по регистрам учёта,
   и роль, которая какой-то из них не видит, не может его записать: база
   отвергнет `insert ... returning` (политика `select` применяется к
   возвращаемым строкам). Проверка до записи превращает ошибку драйвера в
   объяснение — а база остаётся страховкой, не единственным контуром.
4. **Записать** одной транзакцией, снеся прежний результат этого же периода:
   повторный запуск обязан давать то же самое, а не второй комплект ведомостей.

Чего здесь нет и не должно быть до третьей очереди: статусов периода и
переходов, утверждения, отката, блокировок, ретро-дельты, следа расчёта (D025).
Расчёт синхронный — 32 человека считаются мгновенно, очередь пришлось бы
объяснять пользователю зря.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from core.models import EmploymentTerm, PayComponent, Payrun, Payslip, PayslipTotals, Tenant
from core.models import Timesheet as TimesheetRow
from payroll import Employee, PayrollEngine, Timesheet, d, insured_base, uses_insured_hours

from .errors import LedgerAccessDenied, PayrunRefused
from .rules import select_rules

__all__ = ["CalcOutcome", "LedgerAccessDenied", "PayrunRefused", "calculate_period"]

CENT = Decimal("0.01")


def money(value) -> Decimal:
    """Округление до копейки в одном месте.

    Явно, а не «как сложится в numeric(14,2)»: половина округляется вверх, и это
    видно в коде, а не выводится из типа колонки.
    """
    return d(value).quantize(CENT, rounding=ROUND_HALF_UP)


def month_end(period: date) -> date:
    return period.replace(day=calendar.monthrange(period.year, period.month)[1])


@dataclass(frozen=True)
class Case:
    """Один человек на входе расчёта: кто, где и с какими часами."""

    employee_id: UUID
    unit_id: UUID | None
    external_id: str
    employee: Employee
    timesheet: Timesheet
    # Кому какие правила: переопределения бывают на группе и на человеке, и
    # применяются они здесь, а не в движке — движок получает готовый пресет.
    group_id: UUID | None = None


@dataclass(frozen=True)
class CalcOutcome:
    """Что получилось. Показывается человеку после нажатия кнопки."""

    payrun_id: UUID
    preset_code: str
    slips: int
    components: int
    calculated_at: datetime
    ledgers: list[str] = field(default_factory=list)


def collect_cases(tenant_id: UUID, period: date) -> list[Case]:
    """Табели периода вместе с действующими условиями найма.

    Условия версионируются, поэтому берётся версия, действующая **в этом
    месяце**: правка ставки будущим числом не должна менять закрытый расчёт.
    """
    end = month_end(period)
    terms: dict[UUID, EmploymentTerm] = {}
    for term in (
        EmploymentTerm.objects.filter(tenant_id=tenant_id, valid_from__lte=end)
        .exclude(valid_to__lte=period)
        .select_related("group")
        .order_by("valid_from")
    ):
        # order_by возрастающий, поэтому последняя запись побеждает — это и есть
        # «самая поздняя версия, начавшая действовать не позже конца месяца».
        terms[term.employee_id] = term

    cases: list[Case] = []
    missing: list[str] = []
    for sheet in (
        TimesheetRow.objects.filter(tenant_id=tenant_id, period=period)
        .select_related("employee")
        .order_by("employee__last_name", "employee__first_name")
    ):
        term = terms.get(sheet.employee_id)
        if term is None:
            missing.append(sheet.employee.external_id)
            continue
        cases.append(
            Case(
                employee_id=sheet.employee_id,
                # Точка берётся из табеля: человек мог отработать месяц не там,
                # где записан по условиям найма.
                unit_id=sheet.unit_id or term.unit_id,
                external_id=sheet.employee.external_id,
                group_id=term.group_id,
                employee=Employee(
                    ext_id=sheet.employee.external_id,
                    name=f"{sheet.employee.last_name} {sheet.employee.first_name}".strip(),
                    group=term.group.code,
                    scheme=term.scheme or term.group.scheme,
                    base_rate=d(term.base_rate),
                    coefficient=d(term.coefficient),
                    # Регистр учёта берём из базы, а не из пресета: политики
                    # доступа стоят на нём, и второй источник истины здесь
                    # означал бы показ строки не тому человеку.
                    ledger=term.ledger or term.group.ledger,
                ),
                timesheet=Timesheet(
                    hours={k: d(v) for k, v in (sheet.hours or {}).items()},
                    insured_hours=d(sheet.insured_hours),
                    norm_hours=d(sheet.norm_hours),
                    deduction=d(sheet.deduction),
                    cash_payout=d(sheet.cash_payout),
                    # Пусто и ноль здесь разные вещи: пусто значит «правки не
                    # было», и движок сам считает доплату до минимума. Поэтому
                    # None передаётся как None, а не приводится к нулю.
                    manual_correction=(
                        None if sheet.manual_correction is None
                        else d(sheet.manual_correction)
                    ),
                ),
            )
        )

    if missing:
        raise PayrunRefused(
            f"нет условий найма на этот месяц: {len(missing)} чел. "
            "Расчёт без них выглядел бы верным, поэтому не выполняется.",
            details=sorted(missing),
        )
    if not cases:
        raise PayrunRefused(
            "за этот период нет ни одного табеля — считать нечего. "
            "Внесите часы и повторите."
        )
    return cases


def check_schemes(cases: list[Case], preset: dict) -> None:
    """Схема расчёта, которой нет в пресете, — отказ с именами, а не KeyError."""
    unknown = sorted({case.employee.scheme for case in cases} - set(preset["schemes"]))
    if unknown:
        raise PayrunRefused(
            f"в правилах страны нет схем расчёта: {', '.join(unknown)}. "
            "Поправьте группу сотрудников или пресет.",
            details=[c.external_id for c in cases if c.employee.scheme in unknown],
        )


def check_insured_base(cases: list[Case], rules) -> None:
    """База для взносов, разошедшаяся с часами, — отказ, а не тихий расчёт.

    По базе считаются взносы и бруто. Если она отстала от табеля, расчёт всё
    равно проходит и выдаёт правдоподобное неверное число — падения нет, и
    заметить нечем. Это ровно тот случай, ради которого в этом продукте
    отказались от молчаливых умолчаний (конституция, принцип 1).

    Проверяются только схемы, которые базу читают: у курьеров и временных
    работ её нет ни в одной формуле, и блокировать их из-за неё было бы
    отказом на пустом месте.

    Ввод часов держит связь сам (`timesheets.store.set_cell`), поэтому отсюда
    отказ приходит только на данные, пришедшие мимо экрана: импорт, сид,
    правку в базе.
    """
    stale: list[str] = []
    for case in cases:
        preset = rules.preset(group_id=case.group_id, employee_id=case.employee_id)
        scheme = (preset.get("schemes") or {}).get(case.employee.scheme) or {}
        if not uses_insured_hours(scheme):
            continue
        declared = insured_base(case.timesheet.hours, preset.get("hour_types") or {})
        if case.timesheet.insured_hours != declared:
            stale.append(case.external_id)

    if stale:
        raise PayrunRefused(
            f"база для взносов не сходится с часами табеля: {len(stale)} чел. "
            "По ней считаются взносы и бруто, поэтому расчёт по устаревшей базе "
            "выглядел бы верным. Проверьте колонку «База взносов» в табеле.",
            details=sorted(stale),
        )


def check_ledgers(slips: list, visible_ledgers) -> list[str]:
    """Записывать можно только то, что роль видит. Иначе — отказ до записи."""
    used = sorted({component.ledger for _, slip in slips for component in slip.components})
    hidden = [ledger for ledger in used if ledger not in set(visible_ledgers or [])]
    if hidden:
        refusal = LedgerAccessDenied(
            "Ведомость этого периода попадает в регистры учёта, недоступные "
            "вашей роли. Запустить расчёт может тот, кто видит их все."
        )
        refusal.ledgers = hidden
        raise refusal
    return used


def _calculate_all(rules, cases: list[Case]) -> list:
    """Посчитать всех, каждого по его правилам.

    Пресет собирается на пару «группа + человек»: переопределения этих двух
    уровней у разных людей разные. Движок на пресет заводится один раз на
    сочетание — без переопределений это ровно один движок на весь расчёт, то
    есть прежнее поведение слово в слово.
    """
    shared = PayrollEngine(rules.base)
    result = []
    for case in cases:
        preset = rules.preset(group_id=case.group_id, employee_id=case.employee_id)
        engine = shared if preset is rules.base else PayrollEngine(preset)
        result.append((case, engine.calculate(case.employee, case.timesheet)))
    return result


def calculate_period(*, tenant_id: UUID, period: date, visible_ledgers) -> CalcOutcome:
    """Посчитать месяц и сохранить результат. Повторный запуск даёт то же самое."""
    with transaction.atomic():
        tenant = Tenant.objects.filter(pk=tenant_id).first()
        if tenant is None:
            # Не «пустой расчёт»: тенанта не видно — значит, и права на него нет.
            raise PayrunRefused("партнёр недоступен")

        rules = select_rules(tenant_id, tenant.country_code, period)
        cases = collect_cases(tenant_id, period)
        check_schemes(cases, rules.base)
        check_insured_base(cases, rules)

        slips = _calculate_all(rules, cases)
        ledgers = check_ledgers(slips, visible_ledgers)

        return _store(tenant_id, period, rules.code, slips, ledgers)


def _store(tenant_id, period, preset_code, slips, ledgers) -> CalcOutcome:
    """Сохранить расчёт, заменив прежний за тот же период."""
    payrun, _ = Payrun.objects.get_or_create(tenant_id=tenant_id, period=period)
    # Ведомости пересобираются целиком: правка входных данных не должна
    # оставлять в базе строки, посчитанные по прежним.
    Payslip.objects.filter(payrun=payrun).delete()

    rows = [
        Payslip(
            tenant_id=tenant_id, payrun=payrun,
            employee_id=case.employee_id, unit_id=case.unit_id,
            notes=list(slip.notes),
        )
        for case, slip in slips
    ]
    Payslip.objects.bulk_create(rows)

    # Итоги — отдельной таблицей: они посчитаны по всем регистрам сразу и видны
    # только тому, кому видны все регистры строки (миграция 0009, issue #42).
    PayslipTotals.objects.bulk_create([
        PayslipTotals(
            tenant_id=tenant_id, payslip=row,
            net=money(slip.net), gross=money(slip.gross), tax=money(slip.tax),
            contributions=money(slip.contributions), total_cost=money(slip.total_cost),
            to_bank=money(slip.to_bank), to_cash=money(slip.to_cash),
        )
        for row, (_, slip) in zip(rows, slips, strict=True)
    ])

    components = [
        PayComponent(
            tenant_id=tenant_id, payslip=row, code=component.code, title=component.title,
            amount=money(component.amount), ledger=component.ledger,
            channel=component.channel, taxable=component.taxable,
        )
        for row, (_, slip) in zip(rows, slips, strict=True)
        for component in slip.components
    ]
    PayComponent.objects.bulk_create(components)

    calculated_at = timezone.now()
    Payrun.objects.filter(pk=payrun.pk).update(calculated_at=calculated_at)
    # Статус остаётся черновиком намеренно: переходы черновик → посчитан →
    # утверждён и их правила — задача T023, здесь их придумывать нельзя.
    return CalcOutcome(
        payrun_id=payrun.pk, preset_code=preset_code, slips=len(rows),
        components=len(components), calculated_at=calculated_at, ledgers=ledgers,
    )
