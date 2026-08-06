"""
Регрессия на обезличенном наборе. Гоняется всегда, в том числе в CI.

Что проверяет: что движок считает так же, как считал, когда фикстура была
записана. Что НЕ проверяет: совпадение с расчётом бухгалтерии — для этого нужен
настоящий файл, см. `test_serbia_june_2026.py`.
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

TOLERANCE = Decimal("0.05")


def test_fixture_covers_all_schemes(sample_rows):
    check_schemes_covered(sample_rows, minimum=25)


def test_every_row_has_expected_values(sample_rows):
    check_expected_present(sample_rows)


@pytest.mark.parametrize("field", FIELDS)
def test_engine_output_did_not_change(engine, sample_rows, field):
    check_field(engine, sample_rows, field, TOLERANCE)


def test_components_sum_to_net(engine, sample_rows):
    check_components_sum_to_net(engine, sample_rows, TOLERANCE)


def test_ledgers_are_assigned(engine, sample_rows):
    check_ledgers_assigned(engine, sample_rows)
