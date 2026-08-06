"""
Юнит-тесты на правила: проверяем, что движок реагирует на конфигурацию,
а не зашит под Сербию.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from payroll import Employee, PayrollEngine, Timesheet, d
from payroll.presets import apply_overrides

MIN_RATE = Decimal("371")


def employee(scheme="standard", group="kitchen", rate=MIN_RATE, coef=1) -> Employee:
    return Employee(
        ext_id="test", name="Тест Тестович", group=group,
        scheme=scheme, base_rate=d(rate), coefficient=d(coef),
    )


def timesheet(**hours) -> Timesheet:
    insured = d(hours.pop("insured", 176))
    return Timesheet(
        hours={k: d(v) for k, v in hours.items()},
        insured_hours=insured,
        norm_hours=insured,
    )


# --- начисление по часам -----------------------------------------------------

def test_hourly_rate_uses_coefficient(engine):
    slip = engine.calculate(employee(coef="1.181"), timesheet(regular=176))
    hours = next(c for c in slip.components if c.code == "hours.regular")
    assert hours.amount == d("371") * d("1.181") * 176


def test_holiday_hours_paid_with_premium(engine):
    slip = engine.calculate(employee(), timesheet(regular=168, holiday=8))
    holiday = next(c for c in slip.components if c.code == "hours.holiday")
    assert holiday.amount == MIN_RATE * Decimal("1.10") * 8


def test_sick_hours_paid_at_reduced_rate(engine):
    slip = engine.calculate(employee(coef=2), timesheet(regular=156, sick=20))
    sick = next(c for c in slip.components if c.code == "hours.sick")
    assert sick.amount == MIN_RATE * 2 * Decimal("0.65") * 20


# --- доплата до минимума -----------------------------------------------------

def test_minimum_guarantee_tops_up_sick_leave(engine):
    """65% от минималки — ниже минималки, разницу доплачиваем."""
    slip = engine.calculate(employee(), timesheet(regular=32, sick=20))
    topup = next(c for c in slip.components if c.code == "minimum_guarantee")
    assert topup.amount == MIN_RATE * Decimal("0.35") * 20  # = 2597, как в таблице


def test_no_guarantee_when_rate_is_high_enough(engine):
    """При коэффициенте 2 даже 65% выше минималки — доплаты быть не должно."""
    slip = engine.calculate(employee(coef=2), timesheet(regular=156, sick=20))
    assert all(c.code != "minimum_guarantee" for c in slip.components)


def test_underworked_hours_do_not_trigger_topup(engine):
    """Недоработка часов — это просто меньше денег, а не повод доплачивать."""
    slip = engine.calculate(employee(), timesheet(regular=88))
    assert all(c.code != "minimum_guarantee" for c in slip.components)


def test_manual_correction_wins_over_rule(engine):
    """Если бухгалтер поставил правку руками — уважаем её и помечаем листок."""
    ts = timesheet(regular=32, sick=20)
    ts.manual_correction = d(1000)
    slip = engine.calculate(employee(), ts)
    assert any(c.code == "manual_correction" for c in slip.components)
    assert all(c.code != "minimum_guarantee" for c in slip.components)
    assert slip.notes


# --- надбавки ----------------------------------------------------------------

def test_meal_allowance_is_prorated_by_days(engine):
    """Обед считается за рабочий день: половина месяца — половина суммы."""
    def meal(slip):
        return next(c.amount for c in slip.components if c.code == "meal_and_vacation_bonus")

    assert meal(engine.calculate(employee(), timesheet(regular=176))) == 1500
    assert meal(engine.calculate(employee(), timesheet(regular=88))) == 750


def test_half_day_employee_gets_full_meal_for_full_month(engine):
    """У полставки день короче: 88 часов закрывают все 22 дня, обед полный."""
    slip = engine.calculate(employee(scheme="half_time_min_base"), timesheet(regular=88))
    meal = next(c.amount for c in slip.components if c.code == "meal_and_vacation_bonus")
    assert meal == 1500


def test_temporary_scheme_has_no_meal_allowance(engine):
    slip = engine.calculate(employee(scheme="temporary", group="temporary"), timesheet(regular=48))
    assert all(c.code != "meal_and_vacation_bonus" for c in slip.components)


# --- слои учёта --------------------------------------------------------------

def test_layer_comes_from_group(engine):
    """Курьеры в чёрной, офис в белой — свойство группы, а не компании."""
    couriers = engine.calculate(employee(group="couriers"), timesheet(regular=176))
    office = engine.calculate(employee(group="office"), timesheet(regular=176))
    assert next(c.layer for c in couriers.components if c.code == "hours.regular") == "black"
    assert next(c.layer for c in office.components if c.code == "hours.regular") == "white"


def test_employee_layer_overrides_group(engine):
    emp = employee(group="couriers")
    emp.layer = "grey"
    slip = engine.calculate(emp, timesheet(regular=176))
    assert next(c.layer for c in slip.components if c.code == "hours.regular") == "grey"


# --- конфигурируемость -------------------------------------------------------

def test_engine_is_not_hardcoded_to_serbia(serbia_preset):
    """Меняем правила конфигом — результат обязан измениться."""
    tweaked = apply_overrides(serbia_preset, {
        "constants.min_hourly_rate": 500,
        "hour_types.sick.pay_percent": 0.80,
    })
    other = PayrollEngine(tweaked)
    slip = other.calculate(employee(rate=500), timesheet(regular=100, sick=20))
    sick = next(c for c in slip.components if c.code == "hours.sick")
    assert sick.amount == Decimal("500") * Decimal("0.80") * 20


def test_overrides_do_not_mutate_source_preset(serbia_preset):
    apply_overrides(serbia_preset, {"constants.min_hourly_rate": 999})
    assert serbia_preset["constants"]["min_hourly_rate"] == 371.00


def test_unknown_scheme_fails_loudly(engine):
    with pytest.raises(KeyError):
        engine.calculate(employee(scheme="не-существует"), timesheet(regular=176))
