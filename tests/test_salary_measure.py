"""Оклад: вторая форма оплаты рядом с часами и сдельщиной (issue #188).

Владелец 26.08.2026 на вопрос, как считается неполный месяц, ответил дословно:
«либо часы либо сумма оклада». То есть у человека одна из двух форм — либо ему
платят за час, либо он сидит на окладе; до этой задачи продукт умел только
первое, и окладника приходилось заводить выдуманной часовой ставкой.

**Как это устроено.** Оклад — мера работы (`work_measures.salary`), как часы и
доставки, а сумма живёт там же, где ставка, — в условиях найма, версией с даты.
Часовая ставка окладника **выводится**: оклад ÷ норма месяца. Это не уловка, а
единственный способ не заводить второй расчёт: от часовой ставки зависят все
надбавки (ночные, вечерние, выходные), больничный, доплата до минимума и база
взносов. Выведи её один раз — и весь механизм работает как для почасовика.

Отсюда поведение, которое здесь и проверяется: отработал норму — получил ровно
оклад; отработал половину — половину; ночные считаются коэффициентом от
выведенной ставки.

**Второй вариант — «сумма целиком», не зависящая от часов** (`proration:
none`). У части партнёров оклад именно такой. Оба варианта поддержаны и
выбираются правилом, как это принято в проекте для развилок, ответ на которые
ещё не подтверждён бухгалтерией (Q025).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from payroll import Employee, PayrollEngine, Timesheet, d
from payroll.presets import apply_overrides

SALARY = Decimal("90000")
NORM = Decimal("176")


def salaried(rate=SALARY, coef=1, scheme="standard") -> Employee:
    return Employee(
        ext_id="salary-1", name="Окладов Иван", group="office",
        scheme=scheme, base_rate=d(rate), coefficient=d(coef),
        work_measure="salary",
    )


def timesheet(norm=NORM, **hours) -> Timesheet:
    insured = d(hours.pop("insured", sum(Decimal(str(v)) for v in hours.values())))
    return Timesheet(
        hours={k: d(v) for k, v in hours.items()},
        insured_hours=insured,
        norm_hours=d(norm),
    )


def accrued(slip) -> Decimal:
    """Всё начисленное до налогов — то, что человек «заработал за месяц»."""
    return sum(c.amount for c in slip.components if c.code.startswith(("hours.", "salary")))


def test_the_preset_knows_the_salary_measure(serbia_preset):
    measures = serbia_preset["work_measures"]
    assert "salary" in measures, "оклада нет среди мер работы — окладника завести нечем"
    assert measures["salary"].get("monthly") is True, (
        "оклад обязан браться из условий найма, а не из табеля: в табеле лежат часы"
    )


def test_a_full_month_earns_exactly_the_salary(engine):
    """Отработал норму — получил ровно оклад, без копейки расхождения."""
    slip = engine.calculate(salaried(), timesheet(regular=NORM))
    assert accrued(slip) == SALARY


def test_half_a_month_earns_half_the_salary(engine):
    """Вышел в середине месяца — получил половину. Это и есть пропорция."""
    slip = engine.calculate(salaried(), timesheet(regular=NORM / 2))
    assert accrued(slip) == SALARY / 2


def test_night_hours_are_paid_by_the_coefficient_from_the_derived_rate(engine, serbia_preset):
    """Надбавка окладнику считается от выведенной ставки, а не от нуля.

    Без выведенной ставки ночные у окладника не считались бы вовсе: часовой
    ставки у него в условиях найма нет.
    """
    percent = d(serbia_preset["hour_types"]["night"]["pay_percent"])
    slip = engine.calculate(salaried(), timesheet(regular=NORM - 10, night=10))

    night = next(c for c in slip.components if c.code == "hours.night")
    # Порядок тот же, что в движке: деление на норму стоит последним, иначе
    # сравнение спорит с продуктом на хвосте округления, а не по существу.
    assert night.amount == SALARY * percent * 10 / NORM


def test_the_coefficient_of_the_terms_still_multiplies_the_salary(engine):
    """Коэффициент условий найма работает и у оклада: 1,5 оклада — это 1,5 оклада."""
    slip = engine.calculate(salaried(coef=Decimal("1.5")), timesheet(regular=NORM))
    assert accrued(slip) == SALARY * Decimal("1.5")


def test_a_flat_salary_ignores_the_hours(serbia_preset):
    """Второй вариант: сумма целиком, сколько бы часов ни стояло в табеле."""
    flat = PayrollEngine(apply_overrides(serbia_preset, {
        "work_measures.salary.proration": "none",
    }))
    full = flat.calculate(salaried(), timesheet(regular=NORM))
    part = flat.calculate(salaried(), timesheet(regular=NORM / 4))

    assert accrued(full) == SALARY
    assert accrued(part) == SALARY, "при «сумме целиком» часы на оклад не влияют"


def test_the_salary_goes_through_the_scheme_like_any_other_accrual(engine):
    """Оклад — вход расчёта наравне с часами: налоги и взносы считаются от него."""
    slip = engine.calculate(salaried(), timesheet(regular=NORM, insured=NORM))
    assert slip.gross > SALARY, "бруто не посчитано — оклад прошёл мимо схемы"
    assert slip.contributions > 0, "взносы от оклада не посчитаны"


def test_a_month_without_a_norm_is_refused_loudly(engine):
    """Норма ноль — делить не на что. Отказ словами, а не деление на ноль."""
    with pytest.raises(ValueError, match="норм"):
        engine.calculate(salaried(), timesheet(norm=0, regular=10))


# --- оклад на экране табеля ---------------------------------------------------
# Проверки ниже держат вторую половину задачи: движок считает оклад правильно,
# но экран мог бы объявить окладника сдельщиком — тогда его часы были бы
# помечены «не оплачиваются», а в сетке появилась бы лишняя колонка величины.


def test_the_grid_does_not_take_a_salary_for_piecework(serbia_preset):
    """Оклад — не сдельщина: часы окладника оплачены, и экран обязан это знать."""
    from timesheets.grid import pays_by_hours

    class Rules:
        def preset(self, **_kwargs):
            return serbia_preset

    class Term:
        group_id = None
        employee_id = None

    assert pays_by_hours(Rules(), Term(), "salary") is True
    assert pays_by_hours(Rules(), Term(), "deliveries") is False
    assert pays_by_hours(Rules(), Term(), "hours") is True


def test_a_flat_salary_is_not_paid_by_hours(serbia_preset):
    """А «сумма целиком» часами не меряется — там пометка про часы честна."""
    from payroll.presets import apply_overrides
    from timesheets.grid import pays_by_hours

    flat = apply_overrides(serbia_preset, {"work_measures.salary.proration": "none"})

    class Rules:
        def preset(self, **_kwargs):
            return flat

    class Term:
        group_id = None
        employee_id = None

    assert pays_by_hours(Rules(), Term(), "salary") is False
