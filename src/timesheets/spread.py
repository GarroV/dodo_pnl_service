"""
Раскладка месячного числа часов по дням.

Партнёр ведёт итог за месяц (D011), а хранить продукт обязан подневно. Какие
именно дни стоят за числом «176», продукт не знает и знать не может, поэтому
раскладка ровная — по рабочим дням производственного календаря страны.

Единственное свойство, которое здесь по-настоящему важно: **сумма дней в
точности равна введённому числу**. Наивное `total / len(days)` его не даёт —
176 на 22 дня делится нацело, а 100 на 22 уже нет, и часы начали бы усыхать от
самого факта хранения. Поэтому счёт идёт в сотых долях часа целыми числами, а
остаток раздаётся по одной сотой первым дням.
"""
from __future__ import annotations

import calendar as _calendar
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal

__all__ = ["calendar_working_days", "spread", "working_days"]

# Часы храним с двумя знаками, поэтому единица счёта — сотая доля часа.
STEP = Decimal("0.01")
SCALE = 100


def working_days(period: date, holidays: Iterable[date] = ()) -> list[date]:
    """Рабочие дни месяца: будни минус праздники.

    Пятидневка — умолчание, а не правило страны: настоящий календарь приходит
    из таблицы `calendars` (см. `calendar_working_days`). Если его на месяц нет,
    ровная раскладка по будням всё равно лучше, чем отказ сохранить часы.
    """
    holiday_set = set(holidays)
    last = _calendar.monthrange(period.year, period.month)[1]
    first = period.replace(day=1)
    return [
        day
        for day in (first + timedelta(days=offset) for offset in range(last))
        if day.weekday() < 5 and day not in holiday_set
    ]


def calendar_working_days(country_code: str, period: date) -> list[date]:
    """Рабочие дни по производственному календарю страны на этот месяц."""
    from core.models import Calendar

    row = (
        Calendar.objects.filter(country_code=country_code, period=period)
        .values_list("holidays", flat=True)
        .first()
    )
    return working_days(period, holidays=row or ())


def spread(total: Decimal, days: Sequence[date]) -> dict[date, Decimal]:
    """Разложить число часов по дням так, чтобы сумма совпала до сотой.

    Ноль даёт пустой словарь: отсутствие часов — это отсутствие строк, а не
    двадцать две строки с нулём, которые потом придётся отличать от данных.
    """
    if total == 0:
        return {}
    if not days:
        # Молча потерять часы нельзя, а разложить их некуда: месяц без единого
        # рабочего дня — это ошибка календаря, и её нужно увидеть.
        raise ValueError("нет ни одного рабочего дня, часы разложить некуда")
    if total < 0:
        raise ValueError("отрицательных часов не бывает")

    units = int((total / STEP).to_integral_value())
    base, remainder = divmod(units, len(days))
    return {
        day: (base + (1 if index < remainder else 0)) * STEP
        for index, day in enumerate(days)
        # День, на который не пришлось ни одной сотой, не хранится: строка с
        # нулём — это тоже утверждение, и неверное.
        if base + (1 if index < remainder else 0) > 0
    }
