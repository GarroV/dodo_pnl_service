"""
Единственная точка записи в табель.

Данные табеля живут в двух таблицах, и они обязаны сходиться:

* `timesheets` — месячный итог. Его читает движок (`payrun.calc.collect_cases`),
  и там же лежат поля, которых у дня нет и быть не может: база для взносов,
  норма, удержание, выплата наличными, ручная правка бухгалтера.
* `timesheet_days` — из каких дней этот итог сложился (D011).

Инвариант: `timesheets.hours[тип]` равен сумме часов этого типа по дням.
Держится он здесь, а не триггером в базе и не формой на странице. Причина:
писать в табель будут три разных пути — экран, импорт таблицы партнёра и
коннектор Dodo IS, — и договариваться между собой они не обязаны; а триггер
пришлось бы отключать на массовой загрузке, то есть ровно тогда, когда он нужен.

Строка переходит на подневное хранение **целиком и один раз**: при первой правке
любой ячейки раскладываются все типы часов строки, а не только правленый. Иначе
строка осталась бы наполовину подневной, и утверждение «итог равен сумме дней»
перестало бы быть проверяемым.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.db import transaction
from django.db.models import Sum

from core.models import Tenant, Timesheet, TimesheetDay
from payroll import Timesheet as EngineTimesheet
from payroll import d
from payrun.rules import select_rules

from .spread import calendar_working_days, spread

__all__ = [
    "CellRefused", "daily_totals", "hour_types", "materialize", "parse_hours",
    "set_cell", "timesheet_for",
]

# Часы с двумя знаками — как в колонке. Больше не принимаем не из вредности:
# 8,333 часа в базе округлилось бы молча, и введённое перестало бы совпадать с
# показанным.
CENT = Decimal("0.01")

# Верхняя граница ввода. Не бизнес-правило, а защита от опечатки в порядке:
# в месяце физически 744 часа, и «1760» вместо «176» должно быть отказом, а не
# зарплатой в десять раз больше. Подсказки о подозрительном (часов больше нормы
# и прочее) — задача T021, здесь только заведомо невозможное.
MAX_HOURS = Decimal("744")


class CellRefused(Exception):
    """Ячейку записать нельзя. Сообщение показывается человеку как есть."""


def hour_types(tenant_id: UUID, period: date, country_code: str) -> dict[str, dict]:
    """Типы часов страны на этот месяц — колонки сетки.

    Из правил, а не из списка в коде: новая страна не должна требовать правки
    интерфейса. Порядок сохраняется тот, что в пресете.
    """
    rules = select_rules(tenant_id, country_code, period)
    return dict(rules.base.get("hour_types") or {})


def parse_hours(raw: str) -> Decimal:
    """Строка из поля ввода → часы. Всё непонятное — отказ, а не ноль.

    Запятая принимается наравне с точкой: человек с русской или сербской
    раскладкой наберёт именно её, и «8,5», молча ставшее нулём, — самый дорогой
    вид ошибки в этом экране.
    """
    text = (raw or "").strip().replace(",", ".").replace(" ", "").replace(" ", "")
    if not text:
        return Decimal("0")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise CellRefused(f"«{raw}» — это не число часов") from None
    if not value.is_finite():
        raise CellRefused(f"«{raw}» — это не число часов")
    if value != value.quantize(CENT):
        raise CellRefused("часы задаются с точностью до сотой")
    return value.quantize(CENT)


def check_cell(hour_type: str, hours: Decimal, known: dict[str, dict]) -> None:
    if hour_type not in known:
        # Тип, которого нет в правилах страны, движок посчитать не сможет:
        # запись такой ячейки означала бы часы, не попавшие ни в одну сумму.
        raise CellRefused(f"типа часов «{hour_type}» нет в правилах страны")
    if hours < 0:
        raise CellRefused("отрицательных часов не бывает")
    if hours > MAX_HOURS:
        raise CellRefused(f"больше {MAX_HOURS:.0f} часов в месяце не бывает")


def daily_totals(row: Timesheet) -> dict[str, Decimal]:
    """Сколько часов каждого типа лежит в днях этой строки."""
    return {
        item["hour_type"]: item["total"]
        for item in TimesheetDay.objects.filter(timesheet=row)
        .values("hour_type").annotate(total=Sum("hours")).order_by("hour_type")
    }


def country_of(tenant_id: UUID) -> str:
    """Страна тенанта. Берётся у него, а не у календаря: календарь — справочник,
    и искать в нём страну по одному лишь месяцу значило бы получить чужую."""
    return (
        Tenant.objects.filter(pk=tenant_id)
        .values_list("country_code", flat=True)
        .first()
    ) or ""


def _working_days(row: Timesheet) -> list[date]:
    return calendar_working_days(country_of(row.tenant_id), row.period)


def _write_days(row: Timesheet, hour_type: str, hours: Decimal, days, source: str) -> None:
    """Переписать дни одного типа. Прежние сносятся: правка — это замена."""
    TimesheetDay.objects.filter(timesheet=row, hour_type=hour_type).delete()
    parts = spread(hours, days)
    if not parts:
        return
    TimesheetDay.objects.bulk_create([
        TimesheetDay(
            tenant_id=row.tenant_id, timesheet=row, work_date=day,
            hour_type=hour_type, hours=value, source=source,
        )
        for day, value in parts.items()
    ])


@transaction.atomic
def materialize(row: Timesheet) -> bool:
    """Перевести строку на подневное хранение. Идемпотентна.

    Раскладка сохраняет сумму, поэтому месячный итог после неё тот же — на это
    есть тест, и он же охраняет уже принятые суммы расчёта.
    """
    if TimesheetDay.objects.filter(timesheet=row).exists():
        return False

    days = _working_days(row)
    for hour_type, value in (row.hours or {}).items():
        hours = d(value)
        if hours > 0:
            _write_days(row, hour_type, hours, days, source="spread")
    return True


@transaction.atomic
def set_cell(*, timesheet: Timesheet, hour_type: str, hours: Decimal,
             known: dict[str, dict] | None = None) -> Decimal:
    """Записать одну ячейку сетки. Возвращает записанное значение.

    Порядок именно такой: сначала проверка, потом перевод строки на дни, потом
    запись. Отказ обязан ничего не менять — иначе неудачная правка оставляла бы
    строку в непонятном состоянии.
    """
    if known is None:
        known = hour_types(
            timesheet.tenant_id, timesheet.period, country_of(timesheet.tenant_id)
        )
    check_cell(hour_type, hours, known)

    materialize(timesheet)
    _write_days(timesheet, hour_type, hours, _working_days(timesheet), source="manual")

    # Месячный итог пересобирается из дней, а не досчитывается: только так
    # инвариант остаётся утверждением о данных, а не о порядке вызовов.
    totals = daily_totals(timesheet)
    kept = {
        # Типы без дней сохраняются как есть только в одном случае — если дней
        # нет вообще ни у кого (строка пустая). После materialize такого не
        # бывает, поэтому источник итога один: дни.
        kind: str(value.quantize(CENT))
        for kind, value in totals.items()
    }
    timesheet.hours = kept
    Timesheet.objects.filter(pk=timesheet.pk).update(hours=kept)
    return hours


def timesheet_for(employee_id: UUID, period: date) -> EngineTimesheet:
    """То, что принимает движок, собранное из подневного хранения.

    Контракт блока. Часы берутся из дней, когда они есть; остальные поля — со
    строки: базы для взносов и нормы у дня нет.

    Человек, отработавший месяц на двух точках, — это две строки табеля и две
    ведомости (так их и считает `payrun.calc`). Склеивать их здесь в одну
    значило бы дать движку вход, которого он не получит на самом деле, поэтому
    неоднозначность — отказ, а не догадка.
    """
    rows = list(Timesheet.objects.filter(employee_id=employee_id, period=period))
    if not rows:
        raise CellRefused("за этот период у сотрудника нет табеля")
    if len(rows) > 1:
        raise CellRefused(
            "у сотрудника несколько строк табеля за месяц (работа на разных "
            "точках) — уточните точку"
        )
    row = rows[0]

    totals = daily_totals(row)
    hours = totals or {k: d(v) for k, v in (row.hours or {}).items()}
    return EngineTimesheet(
        hours={k: d(v) for k, v in hours.items()},
        insured_hours=d(row.insured_hours),
        norm_hours=d(row.norm_hours),
        deduction=d(row.deduction),
        cash_payout=d(row.cash_payout),
        # Пусто и ноль — разные вещи: пусто значит «правки не было», и движок
        # считает доплату до минимума сам.
        manual_correction=(
            None if row.manual_correction is None else d(row.manual_correction)
        ),
    )
