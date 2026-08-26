"""Коэффициенты за вечерние, ночные и выходные часы (D054).

Владелец 26.08.2026 дословно: «надбавки это коэффициенты за вечерние часы,
ночные часы, выходные и прочее». То есть надбавка живёт **у типа часа**, а не
множителем у человека и не отдельной ставкой — этим закрыт вопрос Q006,
висевший с начала стройки.

Что проверяется здесь:

1. Типы часов вечерних и выходных вообще есть — до этого в наборе были только
   обычные, праздничные, отпуск, больничный, ночные и переработка.
2. Каждый тип начисляется своим коэффициентом отдельным компонентом, а не
   растворяется в общей сумме: иначе в ведомости не видно, из чего сложилась
   выплата.
3. **Коэффициент задаёт партнёр.** Значения в пресете страны — заготовка;
   партнёр меняет их переопределением с даты, и расчёт обязан поехать за
   правкой. Это и есть смысл всей затеи: у каждой сети свои надбавки.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from payroll import Employee, PayrollEngine, Timesheet, d
from payroll.presets import apply_overrides

RATE = Decimal("400")

# Типы часов, у которых надбавка задаётся коэффициентом. Праздничные и
# переработка были в наборе раньше, вечерние и выходные добавлены с D054.
COEFFICIENT_TYPES = ("evening", "night", "weekend", "holiday", "overtime")


def employee(rate=RATE, coef=1) -> Employee:
    return Employee(
        ext_id="test", name="Тест Тестович", group="kitchen",
        scheme="standard", base_rate=d(rate), coefficient=d(coef),
    )


def timesheet(**hours) -> Timesheet:
    insured = d(hours.pop("insured", 176))
    return Timesheet(
        hours={k: d(v) for k, v in hours.items()},
        insured_hours=insured,
        norm_hours=insured,
    )


def test_the_preset_knows_evening_and_weekend_hours(serbia_preset):
    types = serbia_preset["hour_types"]
    for code in COEFFICIENT_TYPES:
        assert code in types, f"типа часов «{code}» нет в наборе — коэффициент задавать нечему"
        assert "pay_percent" in types[code], f"у «{code}» нет коэффициента"


@pytest.mark.parametrize("code", COEFFICIENT_TYPES)
def test_every_coefficient_type_is_accrued_as_its_own_line(engine, serbia_preset, code):
    """Каждый тип — отдельная строка ведомости с собственным коэффициентом."""
    slip = engine.calculate(employee(), timesheet(**{code: 10}))
    line = next((c for c in slip.components if c.code == f"hours.{code}"), None)
    assert line is not None, f"часы «{code}» не попали в ведомость отдельной строкой"

    percent = d(serbia_preset["hour_types"][code]["pay_percent"])
    assert line.amount == RATE * percent * 10


def test_the_partner_sets_the_coefficient_and_the_calculation_follows(serbia_preset):
    """Партнёр ставит свою надбавку — расчёт обязан поехать за ней.

    Проверяется переопределением, потому что именно так партнёр и правит
    правило: тело страны остаётся как есть, поверх ложится его значение с даты.
    """
    theirs = PayrollEngine(apply_overrides(serbia_preset, {
        "hour_types.evening.pay_percent": 1.15,
        "hour_types.weekend.pay_percent": 1.50,
    }))
    slip = theirs.calculate(employee(), timesheet(evening=10, weekend=8))

    evening = next(c for c in slip.components if c.code == "hours.evening")
    weekend = next(c for c in slip.components if c.code == "hours.weekend")
    assert evening.amount == RATE * Decimal("1.15") * 10
    assert weekend.amount == RATE * Decimal("1.50") * 8


def test_the_country_value_is_only_a_default(serbia_preset):
    """Заготовка страны не мешает партнёру: его значение сильнее."""
    country = d(serbia_preset["hour_types"]["night"]["pay_percent"])
    theirs = PayrollEngine(apply_overrides(
        serbia_preset, {"hour_types.night.pay_percent": 1.40},
    ))
    slip = theirs.calculate(employee(), timesheet(night=10))
    night = next(c for c in slip.components if c.code == "hours.night")

    assert night.amount == RATE * Decimal("1.40") * 10
    assert night.amount != RATE * country * 10, "переопределение не подействовало"


def test_evening_and_weekend_hours_count_as_worked(serbia_preset):
    """Вечерние и выходные — отработанные часы, а не отсутствие.

    От этого зависит компенсация питания: она считается по отработанному, и
    человек, отработавший смену вечером, обязан получить её так же, как днём.
    """
    types = serbia_preset["hour_types"]
    for code in ("evening", "weekend"):
        assert types[code]["counts_as_worked"] is True, (
            f"«{code}» не считается отработанным — компенсация питания за такую смену пропадёт"
        )
        assert types[code]["insured"] is True, f"«{code}» не входит в базу взносов"
