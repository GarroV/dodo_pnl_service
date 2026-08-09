"""Сдельная работа курьеров: часы и сдельная величина как два равноправных пути.

Основание — D032, ответ владельца на Q011: чем меряют работу курьера в Сербии,
бухгалтер ещё не сказал, поэтому поддержаны оба способа, а выбор — правило
группы `work_measure`, а не ветка в коде.

Что здесь проверяется и почему именно это:

1. **Умолчание не сдвинулось.** Без `work_measure` и без сдельной величины
   расчёт обязан быть побайтово прежним: на движке стоит сходимость с
   бухгалтерией, и «поддержали второй способ» не должно стоить ни копейки на
   первом.
2. **Оба способа дают разные и объяснимые числа.** Разные — иначе поддержка
   второго пути ничего не значит. Объяснимые — в следе расчёта видно
   количество, ставку и то, каким правилом выбран способ.
3. **Способ — версионируемое правило.** Переключение с новой даты не трогает
   уже посчитанный месяц. Это то же требование, что в T026, и проверяется оно
   на живой базе (`test_timesheets.py` и ниже по файлу), а не только на чистом
   движке.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from payroll import Employee, PayrollEngine, Timesheet
from payroll.presets import apply_overrides

D = Decimal


def courier(scheme: str = "direct") -> Employee:
    return Employee(
        ext_id="c-1", name="Курир Марко", group="couriers", scheme=scheme,
        base_rate=D("420.00"), coefficient=D("1.0"),
    )


def amounts(slip) -> dict[str, Decimal]:
    return {c.code: c.amount for c in slip.components}


# =============================================================================
# 1. Умолчание: ничего не сдвинулось
# =============================================================================


def test_default_measure_is_hours(engine):
    """Группа без `work_measure` считается по часам — как считалась всегда."""
    slip = engine.calculate(courier(), Timesheet(hours={"regular": D(168)}))

    assert amounts(slip) == {"hours.regular": D("420.00") * D(168)}
    assert slip.net == D("420.00") * D(168)


def test_piece_value_without_piecework_measure_changes_nothing(engine):
    """Сдельная величина у почасовой группы денег не даёт.

    Иначе переключение способа было бы необратимым: величина, введённая по
    ошибке, начала бы участвовать в расчёте той группы, где её не спрашивали.
    """
    with_piece = engine.calculate(
        courier(), Timesheet(hours={"regular": D(168)}, piece_value=D(500)),
    )
    without = engine.calculate(courier(), Timesheet(hours={"regular": D(168)}))

    assert amounts(with_piece) == amounts(without)
    assert with_piece.net == without.net


# =============================================================================
# 2. Сдельно: количество × ставка и фиксированная сумма
# =============================================================================


def piecework_engine(preset, measure: str) -> PayrollEngine:
    return PayrollEngine(
        apply_overrides(preset, {"groups.couriers.work_measure": measure})
    )


def test_deliveries_pay_quantity_times_rate(serbia_preset):
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(courier(), Timesheet(piece_value=D(120)))

    assert amounts(slip) == {"piecework.deliveries": D("420.00") * D(120)}
    assert slip.net == D("50400.00")
    # Схема `direct`: бруто равно нето, взносов нет — мера работы этого не меняет.
    assert slip.gross == slip.net
    assert slip.contributions == D(0)
    assert slip.total_cost == slip.net


def test_fixed_amount_is_the_amount_itself(serbia_preset):
    """У фиксированной выплаты ставка не применяется вовсе.

    Величина в табеле — сама сумма. Умножить её на ставку значило бы получить
    правдоподобное число в сотни раз больше настоящего.
    """
    engine = piecework_engine(serbia_preset, "fixed_amount")

    slip = engine.calculate(courier(), Timesheet(piece_value=D("45000.00")))

    assert amounts(slip) == {"piecework.fixed_amount": D("45000.00")}
    assert slip.net == D("45000.00")


def test_two_measures_give_different_numbers(serbia_preset):
    """Оба способа на одних и тех же данных дают разное — иначе выбирать нечего."""
    ts = lambda: Timesheet(hours={"regular": D(168)}, piece_value=D(120))  # noqa: E731

    by_hours = PayrollEngine(serbia_preset).calculate(courier(), ts())
    by_pieces = piecework_engine(serbia_preset, "deliveries").calculate(courier(), ts())
    by_fixed = piecework_engine(serbia_preset, "fixed_amount").calculate(courier(), ts())

    assert by_hours.net == D("70560.00")   # 168 ч × 420
    assert by_pieces.net == D("50400.00")  # 120 доставок × 420
    assert by_fixed.net == D("120.00")     # сама величина
    assert len({by_hours.net, by_pieces.net, by_fixed.net}) == 3


def test_hours_of_a_piecework_group_are_not_paid_and_not_silent(serbia_preset):
    """Часы сдельной группы не оплачиваются — и об этом сказано, а не умолчано.

    Молчание здесь было бы дорогим: человек видит в табеле 168 часов и ждёт за
    них денег, а их нет. Пометка уходит в ведомость (`payslips.notes`).
    """
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(
        courier(), Timesheet(hours={"regular": D(168)}, piece_value=D(120)),
    )

    assert amounts(slip) == {"piecework.deliveries": D("50400.00")}
    assert any("сдельно" in note for note in slip.notes), slip.notes


def test_no_note_when_there_are_no_hours(serbia_preset):
    """Пометка появляется от часов, а не от самого факта сдельной оплаты."""
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(courier(), Timesheet(piece_value=D(120)))

    assert slip.notes == []


def test_zero_piece_value_gives_no_component(serbia_preset):
    """Ноль — это отсутствие начисления, а не компонент на ноль."""
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(courier(), Timesheet(piece_value=D(0)))

    assert slip.components == []
    assert slip.net == D(0)


def test_unknown_measure_is_loud(serbia_preset):
    """Опечатка в правиле — громкий отказ, а не расчёт мимо неё.

    Тихо посчитать по часам значило бы выдать правдоподобное неверное число:
    правило в базе заведено, в списке видно, а расчёт идёт мимо него.
    """
    engine = piecework_engine(serbia_preset, "per_delivery")

    with pytest.raises(ValueError, match="per_delivery"):
        engine.calculate(courier(), Timesheet(piece_value=D(120)))


# =============================================================================
# 3. След расчёта: из чего собрано число
# =============================================================================


def test_trace_explains_the_piecework_amount(serbia_preset):
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(courier(), Timesheet(piece_value=D(120)))

    step = next(s for s in slip.trace if s.rule_code == "piecework.deliveries")
    # Путь — туда, где сделан ВЫБОР способа: именно его переопределяет партнёр,
    # и именно его версию надо показать в следе.
    assert step.rule_code_path == "groups.couriers.work_measure"
    assert step.input_values["measure"] == "deliveries"
    assert step.input_values["quantity"] == D(120)
    assert step.input_values["rate"] == D("420.00")
    assert step.input_values["pay_per_unit"] is True
    assert step.applied_value == D("50400.00")


def test_trace_of_fixed_amount_does_not_pretend_there_is_a_rate(serbia_preset):
    engine = piecework_engine(serbia_preset, "fixed_amount")

    slip = engine.calculate(courier(), Timesheet(piece_value=D("45000.00")))

    step = next(s for s in slip.trace if s.rule_code == "piecework.fixed_amount")
    assert step.input_values["pay_per_unit"] is False
    assert step.input_values["rate"] is None
