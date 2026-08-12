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

Вместе с часами здесь держится и база для взносов (`insured_hours`) — вход
движка наравне с ними. Правило узкое: связь, которая была, сохраняется. Подробно
объяснено в `set_cell`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.db import transaction
from django.db.models import Sum
from django.utils.timezone import now
from django.utils.translation import gettext as _

from core.models import Tenant, Timesheet, TimesheetDay
from payroll import Timesheet as EngineTimesheet
from payroll import d, insured_base
from payrun.rules import select_rules

from .spread import calendar_working_days, spread

__all__ = [
    "CellRefused", "RowInput", "daily_totals", "hour_types", "insured_base",
    "materialize", "parse_hours", "parse_piece", "row_differs", "set_cell",
    "set_piece", "store_row", "timesheet_for",
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
        raise CellRefused(_("«%(value)s» — это не число часов") % {"value": raw}) from None
    if not value.is_finite():
        raise CellRefused(_("«%(value)s» — это не число часов") % {"value": raw})
    if value != value.quantize(CENT):
        raise CellRefused(_("часы задаются с точностью до сотой"))
    return value.quantize(CENT)


# Верхняя граница сдельной величины — та же защита от опечатки в порядке, что у
# часов, и по той же причине она не бизнес-правило: сколько доставок бывает в
# месяце и какой бывает фиксированная выплата, знает партнёр, а не продукт.
# Миллион отсекает лишний ноль, но не отсекает настоящую сумму в динарах.
MAX_PIECE = Decimal("1000000")


def parse_piece(raw: str) -> Decimal:
    """Строка из поля ввода → сдельная величина. Разбор тот же, что у часов.

    Намеренно та же функция, а не своя: запятая вместо точки, лишний пробел и
    мусор вместо числа ведут себя одинаково в обеих колонках. Отличается только
    проверка границ — она ниже, в `set_piece`.
    """
    return parse_hours(raw)


def check_piece(value: Decimal) -> None:
    if value < 0:
        raise CellRefused(_("отрицательной сдельной величины не бывает"))
    if value > MAX_PIECE:
        raise CellRefused(
            _("больше %(limit)s за месяц не бывает — проверьте, не лишний ли ноль")
            % {"limit": f"{MAX_PIECE:.0f}"}
        )


@transaction.atomic
def set_piece(*, timesheet: Timesheet, value: Decimal) -> Decimal:
    """Записать сдельную величину строки табеля (T075). Возвращает записанное.

    По дням не раскладывается, в отличие от часов: за месячным числом доставок
    настоящих дат нет, и ровная раскладка выдумала бы их (см. комментарий у
    колонки `timesheets.piece_value`). Инвариант «итог равен сумме дней»
    касается часов и этой величины не касается вовсе.

    База для взносов здесь не пересчитывается ни при каких условиях: она
    считается из **часов**, входящих в базу по правилам страны, а сдельная
    величина часами не является. Связь, которую бережёт `set_cell`, сдельная
    правка не рвёт и не создаёт.
    """
    check_piece(value)
    Timesheet.objects.filter(pk=timesheet.pk).update(piece_value=value)
    timesheet.piece_value = value
    return value


def check_cell(hour_type: str, hours: Decimal, known: dict[str, dict]) -> None:
    if hour_type not in known:
        # Тип, которого нет в правилах страны, движок посчитать не сможет:
        # запись такой ячейки означала бы часы, не попавшие ни в одну сумму.
        raise CellRefused(_("типа часов «%(kind)s» нет в правилах страны") % {"kind": hour_type})
    if hours < 0:
        raise CellRefused(_("отрицательных часов не бывает"))
    if hours > MAX_HOURS:
        raise CellRefused(
            _("больше %(limit)s часов в месяце не бывает") % {"limit": f"{MAX_HOURS:.0f}"}
        )


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

    # База для взносов шла за часами? Проверяется ДО записи: после неё сумма
    # часов уже другая, и связь было бы не отличить от совпадения.
    base_tracked_hours = (
        d(timesheet.insured_hours) == insured_base(timesheet.hours or {}, known)
    )

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
    changes: dict = {"hours": kept}

    # База для взносов — вход движка наравне с часами: по ней считаются взносы
    # и бруто. Оставить её от прежних часов значило бы получить правдоподобно
    # неверный расчёт молча — ровно то, чего конституция не разрешает.
    #
    # Но и пересчитывать её всегда нельзя: у бухгалтера это отдельная колонка,
    # и она может быть задана независимо от отработанного (Q005). Поэтому
    # правило узкое: связь, которая была, сохраняется; связи не было —
    # ничего не выдумываем, а расхождение показывает сетка и на нём
    # отказывается считать `payrun.calc.check_insured_base`.
    if base_tracked_hours:
        changes["insured_hours"] = insured_base(kept, known)
        timesheet.insured_hours = changes["insured_hours"]

    timesheet.hours = kept
    Timesheet.objects.filter(pk=timesheet.pk).update(**changes)
    return hours


@dataclass(frozen=True)
class RowInput:
    """Строка табеля так, как её приносит внешний источник (импорт, Dodo IS).

    Отдельный тип, а не словарь: полей много, порядок у них не запоминается, и
    перепутанные местами «удержание» и «наличные» дали бы правдоподобно неверную
    зарплату молча.
    """

    hours: dict[str, Decimal]
    insured_hours: Decimal
    norm_hours: Decimal
    deduction: Decimal = Decimal("0")
    cash_payout: Decimal = Decimal("0")
    manual_correction: Decimal | None = None


def _same(left, right) -> bool:
    """Числовое сравнение, а не строковое: «176» и «176.00» — одно число."""
    return d(left) == d(right)


def row_differs(row: Timesheet, want: RowInput) -> bool:
    """Отличается ли строка в базе от того, что принёс источник.

    Нужна ради идемпотентности загрузки (T020): совпало — **не пишем вовсе**.
    Просто переписать теми же числами недостаточно: `_write_days` сносит дни и
    создаёт заново, то есть у них меняются `id` и `created_at`, — и обещание
    «повторная загрузка ничего не меняет» стало бы неправдой при равных числах.
    """
    stored = row.hours or {}
    for kind in set(stored) | set(want.hours):
        if not _same(stored.get(kind, 0), want.hours.get(kind, 0)):
            return True
    if not _same(row.insured_hours, want.insured_hours):
        return True
    if not _same(row.norm_hours, want.norm_hours):
        return True
    if not _same(row.deduction, want.deduction):
        return True
    if not _same(row.cash_payout, want.cash_payout):
        return True
    # Пусто и ноль здесь разные вещи: пусто значит «правки не было», и движок
    # считает доплату до минимума сам.
    if (row.manual_correction is None) != (want.manual_correction is None):
        return True
    if row.manual_correction is not None and not _same(
        row.manual_correction, want.manual_correction
    ):
        return True
    return False


@transaction.atomic
def store_row(*, timesheet: Timesheet, want: RowInput,
              known: dict[str, dict] | None = None,
              actor_id: UUID | None = None, reason: str = "",
              source: str = "import") -> bool:
    """Записать строку табеля целиком. Возвращает True, если что-то изменилось.

    Почему не циклом из `set_cell`. Во-первых, `set_cell` бережёт связь «база
    для взносов идёт за часами» (решение 9), а у источника база **своя**: в
    таблице партнёра это отдельная колонка, и подменять её пересчётом значило бы
    выбросить единственное место, где на Q005 есть ответ. Во-вторых, каждая
    ячейка отдельно означала бы N перезаписей дней вместо одной.

    Типы часов, которых источник не принёс, **не трогаются**: чужая таблица не
    знает про ночные и переработку, и обнулять их «за компанию» — значит терять
    введённое человеком.
    """
    if known is None:
        known = hour_types(
            timesheet.tenant_id, timesheet.period, country_of(timesheet.tenant_id)
        )
    for kind, hours in want.hours.items():
        check_cell(kind, hours, known)

    if want.manual_correction is not None and (actor_id is None or not reason.strip()):
        # То же требование, что у ограничения базы (`timesheets_correction_trace_check`,
        # D025). Проверяется здесь заранее, чтобы человек прочитал причину, а не
        # текст нарушения ограничения.
        raise CellRefused(
            _("ручную правку нельзя записать без следа: нужен автор и причина")
        )

    if not row_differs(timesheet, want):
        return False

    materialize(timesheet)
    days = _working_days(timesheet)
    for kind, hours in want.hours.items():
        _write_days(timesheet, kind, hours, days, source=source)

    # Итог пересобирается из дней — тем же способом, что в `set_cell`: источник
    # месячного числа один, иначе инвариант становится утверждением о порядке
    # вызовов, а не о данных.
    kept = {kind: str(value.quantize(CENT)) for kind, value in daily_totals(timesheet).items()}
    changes = {
        "hours": kept,
        "insured_hours": want.insured_hours,
        "norm_hours": want.norm_hours,
        "deduction": want.deduction,
        "cash_payout": want.cash_payout,
        "manual_correction": want.manual_correction,
        "source": source,
    }
    if want.manual_correction is not None:
        changes["correction_reason"] = reason
        changes["corrected_by"] = actor_id
        changes["corrected_at"] = now()

    Timesheet.objects.filter(pk=timesheet.pk).update(**changes)
    for name, value in changes.items():
        setattr(timesheet, name, value)
    return True


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
        piece_value=d(row.piece_value),
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
