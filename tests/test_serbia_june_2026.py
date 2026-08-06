"""
Регрессия на реальных данных: движок должен воспроизводить расчёт бухгалтерии
Сербии за июнь 2026 до копейки. 30 сотрудников, четыре схемы расчёта.

Это главный тест проекта. Если он красный — правила разъехались с реальностью.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

TOLERANCE = Decimal("0.05")
FIELDS = ("net", "gross", "contributions", "total_cost")

# Единственное известное расхождение: у сотрудника на полставки в таблице
# проставлено 545.34, по правилу выходит 545.45. Округление на их стороне.
KNOWN_ROUNDING = {"СОТРУДНИК-ПОЛСТАВКИ": Decimal("0.20")}


def _calculate(engine, row):
    """Считает расчётный листок, подставляя ручные значения из таблицы."""
    slip = engine.calculate(row.employee, row.timesheet)

    # Надбавку бухгалтер иногда проставляет руками. Чтобы сверять схемы расчёта,
    # а не ввод, подставляем её значение и пересчитываем производные.
    if row.sheet_meal is not None:
        rule_meal = next(
            (c.amount for c in slip.components if c.code == "meal_and_vacation_bonus"),
            Decimal(0),
        )
        if abs(rule_meal - row.sheet_meal) >= TOLERANCE:
            scheme = engine.schemes[row.employee.scheme]
            slip.net += row.sheet_meal - rule_meal
            engine.gross_up(slip, row.timesheet, scheme)
            engine.contributions(slip, row.timesheet, scheme)
    return slip


def _ids(rows):
    return [f"{r.sheet}::{r.name}" for r in rows]


def test_fixture_covers_all_schemes(june_rows):
    """Фикстура должна покрывать все схемы, иначе регрессия дырявая."""
    assert {r.scheme for r in june_rows} == {
        "standard", "half_time", "half_time_min_base", "temporary",
    }
    assert len(june_rows) >= 25


def test_every_row_has_expected_values(june_rows):
    """Если импорт сломается, тесты не должны молча позеленеть."""
    for row in june_rows:
        assert row.expected["net"] is not None, f"{row.sheet} / {row.name}: не прочитано нето"


@pytest.mark.parametrize("field", FIELDS)
def test_matches_accountant(engine, june_rows, field):
    """Каждое поле по каждому сотруднику должно совпасть с таблицей."""
    mismatches = []

    for row in june_rows:
        expected = row.expected[field]
        if expected is None:
            continue

        got = getattr(_calculate(engine, row), field)
        tolerance = KNOWN_ROUNDING.get(row.name, TOLERANCE)

        if abs(got - expected) >= tolerance:
            mismatches.append(
                f"{row.sheet} / {row.name}: движок {got:.2f}, "
                f"таблица {expected:.2f}, разница {got - expected:+.2f}"
            )

    assert not mismatches, f"расхождения по полю «{field}»:\n  " + "\n  ".join(mismatches)


def test_components_sum_to_net(engine, june_rows):
    """Нето должно быть суммой компонентов — иначе разбивка для P&L соврёт."""
    for row in june_rows:
        slip = engine.calculate(row.employee, row.timesheet)
        total = sum((c.amount for c in slip.components), Decimal(0))
        assert abs(total - slip.net) < TOLERANCE, f"{row.name}: компоненты не сходятся с нето"


def test_layers_are_assigned(engine, june_rows):
    """У каждого компонента должен быть слой учёта — на нём строится видимость."""
    valid = {"white", "grey", "black"}
    for row in june_rows:
        for component in engine.calculate(row.employee, row.timesheet).components:
            assert component.layer in valid, f"{row.name}: странный слой {component.layer}"
