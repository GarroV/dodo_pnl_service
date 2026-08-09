"""
Генератор «таблицы бухгалтера» демо-стенда (`demo.accountant_table`).

Экран сверки в демо принимает xlsx в формате PLATA и сравнивает его с
расчётом продукта. Файл для показа генерируется нами же, поэтому здесь
проверяется не то, что генератор «выглядит правильно», а то, что его выход
**фактически** проходит через настоящий разбор `payroll.importers.plata_xlsx`
без единой находки по данным — findings о незаполненных листах формата
допустимы и ожидаемы (мы заполняем не все восемь листов), но findings о
колонке, строке или значении означали бы, что демо-файл сам не проходит
собственный формат.
"""
from __future__ import annotations

from decimal import Decimal

import openpyxl
import pytest

from demo.accountant_table import TableRow, build_accountant_table
from payroll.importers import read_plata_file
from payroll.importers.plata_xlsx import SHEET_MAP

D = Decimal

STANDARD_SHEET = "NS 1 Bulevar "                    # standard
MIN_BASE_SHEET = "NS pola radnog  vremena puno "    # half_time_min_base


def standard_row(**overrides) -> TableRow:
    """Строка на листе `standard` с полным набором чисел, включая копейки."""
    fields = dict(
        first="Ana", last="Petric",
        sheet=STANDARD_SHEET, scheme="standard",
        coefficient=D("1.35"), base_rate=D("720.00"), insured=D("176"),
        regular=D("176"), holiday=D("8"), vacation=D("0"), sick=D("0"),
        deduction=D("0"), cash=D("0"),
        correction=None, meal=D("1500.00"),
        net=D("65432.10"), gross=D("91234.56"),
        contributions=D("12345.67"), total_cost=D("103580.23"),
    )
    fields.update(overrides)
    return TableRow(**fields)


def min_base_row(**overrides) -> TableRow:
    """Строка на листе `half_time_min_base` — у него свой заголовок бруто."""
    fields = dict(
        first="Iva", last="Markovic",
        sheet=MIN_BASE_SHEET, scheme="half_time_min_base",
        coefficient=D("1.00"), base_rate=D("450.00"), insured=D("88"),
        regular=D("88"), holiday=D("0"), vacation=D("0"), sick=D("0"),
        deduction=D("0"), cash=D("0"),
        correction=D("500.00"), meal=None,
        net=D("39600.00"), gross=D("52800.00"),
        contributions=D("6600.00"), total_cost=D("59400.00"),
    )
    fields.update(overrides)
    return TableRow(**fields)


@pytest.fixture
def generated_file(tmp_path):
    """Сгенерированный файл на диске — так же его получит `read_plata_file`."""
    rows = [standard_row(), min_base_row()]
    path = tmp_path / "accountant_table.xlsx"
    path.write_bytes(build_accountant_table(rows))
    return path


# =============================================================================
# 1. Файл читается собственным форматом без находок по данным
# =============================================================================


def test_generated_file_has_no_data_findings(generated_file):
    parsed = read_plata_file(generated_file)

    assert len(parsed.rows) == 2

    # column / row / value — находки о том, что мы сами не смогли прочитать
    # свою же запись. Их быть не должно вовсе.
    data_findings = [f for f in parsed.findings if f.kind != "sheet"]
    assert data_findings == [], [f.text for f in data_findings]


def test_unfilled_sheets_are_reported_as_missing_and_nothing_else(generated_file):
    """Находки о листах, которые мы не заполнили, — допустимы и ожидаемы.

    Явный ассерт нужен затем, чтобы не спутать «мы это заложили» с «сверка
    молча проглотила лишние находки»: множество отсутствующих листов обязано
    быть ровно тем, что мы не заполнили, — ни больше, ни меньше.
    """
    parsed = read_plata_file(generated_file)

    filled = {STANDARD_SHEET, MIN_BASE_SHEET}
    expected_missing = {sheet.strip() for sheet in SHEET_MAP if sheet not in filled}

    sheet_findings = {f.where for f in parsed.findings if f.kind == "sheet"}
    assert sheet_findings == expected_missing
    assert len(parsed.findings) == len(expected_missing), (
        "находок оказалось больше, чем ожидаемых «лист не найден»"
    )


def test_default_openpyxl_sheet_is_not_left_in_the_book(generated_file):
    wb = openpyxl.load_workbook(generated_file)
    assert wb.sheetnames == [STANDARD_SHEET, MIN_BASE_SHEET]


# =============================================================================
# 2. Числа не теряют копейки, пустое — не ноль, ext_id — ключ сверки
# =============================================================================


def test_numbers_round_trip_without_losing_a_penny(generated_file):
    parsed = read_plata_file(generated_file)
    by_name = {row.name: row for row in parsed.rows}

    ana = by_name["Ana Petric"]
    assert ana.employee.base_rate == D("720.00")
    assert ana.employee.coefficient == D("1.35")
    assert ana.timesheet.hours["regular"] == D("176")
    assert ana.timesheet.hours["holiday"] == D("8")
    assert ana.timesheet.insured_hours == D("176")
    assert ana.sheet_meal == D("1500.00")
    assert ana.expected["net"] == D("65432.10")
    assert ana.expected["gross"] == D("91234.56")
    assert ana.expected["contributions"] == D("12345.67")
    assert ana.expected["total_cost"] == D("103580.23")

    iva = by_name["Iva Markovic"]
    assert iva.employee.base_rate == D("450.00")
    assert iva.timesheet.hours["regular"] == D("88")
    assert iva.timesheet.manual_correction == D("500.00")
    assert iva.expected["net"] == D("39600.00")
    assert iva.expected["gross"] == D("52800.00")
    assert iva.expected["contributions"] == D("6600.00")
    assert iva.expected["total_cost"] == D("59400.00")


def test_ext_id_is_first_and_last_name(generated_file):
    """Ключ сверки — `ext_id`. Съехавший ключ развалил бы сопоставление строк."""
    parsed = read_plata_file(generated_file)
    by_name = {row.name: row for row in parsed.rows}

    assert by_name["Ana Petric"].employee.ext_id == "Ana Petric"
    assert by_name["Iva Markovic"].employee.ext_id == "Iva Markovic"


def test_empty_field_is_read_as_absent_not_zero(generated_file):
    """`None` в поле — пустая колонка, а не ноль (иначе сверка сравнит с нулём)."""
    parsed = read_plata_file(generated_file)
    by_name = {row.name: row for row in parsed.rows}

    ana = by_name["Ana Petric"]
    assert ana.timesheet.manual_correction is None

    iva = by_name["Iva Markovic"]
    assert iva.sheet_meal is None


# =============================================================================
# 3. Схема листа определяется листом, а не строкой
# =============================================================================


def test_row_with_a_scheme_foreign_to_its_sheet_is_rejected():
    """standard-лист не должен принять строку схемы temporary молча.

    Молча — значит записать заголовок бруто «BRUTO» (он у standard и temporary
    один и тот же, но не факт для другой пары схем), и файл прочитается так,
    будто строка честно посчитана по схеме листа. Дешевле уронить сборку.
    """
    bad_row = standard_row(scheme="temporary")
    with pytest.raises(ValueError, match="схема"):
        build_accountant_table([bad_row])


def test_row_on_a_sheet_outside_the_format_is_rejected():
    bad_row = standard_row(sheet="Заметки бухгалтера")
    with pytest.raises(ValueError, match="PLATA"):
        build_accountant_table([bad_row])
