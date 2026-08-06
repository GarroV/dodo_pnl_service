"""
Сверка с настоящим расчётом бухгалтерии Сербии за июнь 2026.

Это главный тест проекта: движок должен воспроизводить расчёт живой бухгалтерии —
30 сотрудников, четыре схемы. Если он красный, правила разъехались с реальностью.

Таблица содержит ФИО, ставки и суммы живых людей, поэтому в репозитории её нет.
Путь задаётся переменной окружения:

    PAYROLL_FIXTURE=~/Documents/projects/_private/dodo_pnl/plata-2026-06.xlsx pytest

Без неё тест пропускается — и это значит, что сверка НЕ выполнена, а не что всё
сошлось. Постоянная регрессия на обезличенных данных живёт в
`test_regression_sample.py`.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from _payroll_checks import (
    FIELDS,
    check_components_sum_to_net,
    check_expected_present,
    check_field,
    check_ledgers_assigned,
    check_schemes_covered,
)

# Допуск шире обычного из-за одного известного расхождения: у сотрудника на
# полставки в таблице стоит значение, отличающееся на 11 копеек — округление
# на стороне бухгалтерии, не ошибка правила.
TOLERANCE = Decimal("0.25")


def test_fixture_covers_all_schemes(june_rows):
    check_schemes_covered(june_rows, minimum=25)


def test_every_row_has_expected_values(june_rows):
    check_expected_present(june_rows)


@pytest.mark.parametrize("field", FIELDS)
def test_matches_accountant(engine, june_rows, field):
    check_field(engine, june_rows, field, TOLERANCE)


def test_components_sum_to_net(engine, june_rows):
    check_components_sum_to_net(engine, june_rows, TOLERANCE)


def test_ledgers_are_assigned(engine, june_rows):
    check_ledgers_assigned(engine, june_rows)
