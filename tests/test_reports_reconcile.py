"""Сверка расчёта с таблицей бухгалтера (T031).

Что здесь проверяется и почему именно так.

**Совпадение — до копейки.** Никаких «почти сошлось»: допуск, зашитый в
сравнение, однажды спрячет настоящее расхождение ровно того же размера.
Расхождение меньше динара показывается отдельной группой «округление» —
видно, но не мешает читать существенное.

**Наша сторона не выдумывается.** Строка, итогов которой база не отдала,
уходит в «нет в расчёте» без единого нашего числа. Иначе разница между
итогом файла и суммой видимых компонентов выдала бы скрытый регистр
вычитанием — ровно так устроены две уже закрытые утечки (T050, T071).

**Причина расхождения названа входами.** Бухгалтеру мало «не сошлось на 812»:
ему нужно знать, разошлись часы, ставка и надбавка — или сошлись, и тогда
разошлось правило.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from payroll.engine import Employee, Timesheet
from payroll.importers import Finding
from payroll.importers.plata_xlsx import ImportedRow
from reports.reconcile import RunLine, compare

D = Decimal


def file_row(
    name: str = "MARKO JOVANOVIC",
    *,
    net="1000.00", gross="1400.00", contributions="500.00", total_cost="1900.00",
    hours=None, insured="176", rate="371", coefficient="1", meal=None,
    sheet="NS kancelarija",
) -> ImportedRow:
    """Строка таблицы партнёра — ровно то, что отдаёт разбор файла."""
    return ImportedRow(
        sheet=sheet, scheme="standard", group="office", name=name,
        employee=Employee(
            ext_id=name, name=name, group="office", scheme="standard",
            base_rate=D(rate), coefficient=D(coefficient),
        ),
        timesheet=Timesheet(
            hours=hours if hours is not None else {"regular": D("176")},
            insured_hours=D(insured), norm_hours=D(insured),
        ),
        sheet_meal=None if meal is None else D(meal),
        expected={
            "net": None if net is None else D(net),
            "gross": None if gross is None else D(gross),
            "contributions": None if contributions is None else D(contributions),
            "total_cost": None if total_cost is None else D(total_cost),
        },
    )


def run_line(
    name: str = "MARKO JOVANOVIC",
    *,
    net="1000.00", gross="1400.00", contributions="500.00", total_cost="1900.00",
    hours=None, insured="176", rate="371", coefficient="1", meal=None,
) -> RunLine:
    """Строка расчёта — то, что база отдала этой роли."""
    return RunLine(
        key=name, name=name,
        totals={
            "net": D(net), "gross": D(gross),
            "contributions": D(contributions), "total_cost": D(total_cost),
        },
        hours=hours if hours is not None else {"regular": D("176")},
        insured_hours=D(insured),
        base_rate=D(rate), coefficient=D(coefficient),
        meal=None if meal is None else D(meal),
    )


def only(lines):
    assert len(lines) == 1, f"ожидалась одна строка, пришло {len(lines)}"
    return lines[0]


def amount_of(line, code: str):
    return next(a for a in line.amounts if a.code == code)


# --- совпадение и расхождение ------------------------------------------------


def test_equal_row_is_a_match():
    result = compare([file_row()], {"MARKO JOVANOVIC": run_line()})

    line = only(result.lines)
    assert line.matched
    assert result.matched == 1 and result.mismatched == 0
    assert all(a.matches for a in line.amounts)


def test_difference_is_shown_with_both_numbers_and_its_sign():
    """Бухгалтеру нужны оба числа и знак, а не только «не сошлось»."""
    result = compare([file_row(net="1000.00")], {"MARKO JOVANOVIC": run_line(net="1812.50")})

    line = only(result.lines)
    assert not line.matched
    net = amount_of(line, "net")
    assert net.expected == D("1000.00") and net.actual == D("1812.50")
    assert net.diff == D("812.50")
    assert result.mismatched == 1


def test_a_kopeck_is_not_a_match_but_is_named_rounding():
    """Одиннадцать копеек — известное расхождение округления, а не совпадение."""
    result = compare([file_row(net="1000.00")], {"MARKO JOVANOVIC": run_line(net="1000.11")})

    line = only(result.lines)
    assert not line.matched, "копейки не должны выдаваться за совпадение"
    assert line.rounding_only, "копеечное расхождение обязано быть названо округлением"
    assert amount_of(line, "net").rounding
    assert result.rounding == 1 and result.mismatched == 0


def test_a_whole_dinar_is_no_longer_rounding():
    result = compare([file_row(net="1000.00")], {"MARKO JOVANOVIC": run_line(net="1001.00")})

    assert not only(result.lines).rounding_only
    assert result.mismatched == 1 and result.rounding == 0


def test_field_missing_in_the_file_is_not_compared_as_zero():
    """Не прочитанное из файла поле — не ноль. Иначе сверка врёт на всю сумму."""
    result = compare([file_row(gross=None)], {"MARKO JOVANOVIC": run_line()})

    gross = amount_of(only(result.lines), "gross")
    assert gross.expected is None and not gross.comparable
    assert not gross.matches and gross.diff is None
    assert only(result.lines).matched, "остальные поля сошлись — строка сошлась"


# --- чего наша сторона не делает ---------------------------------------------


def test_a_row_the_run_did_not_give_us_carries_no_numbers_of_ours():
    """Главная проверка D023: по невидимой строке мы не показываем ничего.

    Ни суммы, ни разницы. Разница между итогом файла и суммой видимых
    компонентов — это и есть скрытый регистр, выданный вычитанием.
    """
    result = compare([file_row(name="PETAR PETROVIC")], {})

    assert not result.lines
    absent = only(result.only_in_file)
    assert absent.name == "PETAR PETROVIC"
    assert not hasattr(absent, "diff")
    assert "PETAR PETROVIC" not in {line.name for line in result.lines}


def test_a_row_present_only_in_the_run_is_named_too():
    result = compare([], {"ANA ANIC": run_line("ANA ANIC")})

    assert only(result.only_in_run).name == "ANA ANIC"
    assert not result.lines


def test_matching_goes_by_key_not_by_name():
    """Однофамильцы не должны схлопываться: ключ — external_id, а не ФИО."""
    theirs = file_row(name="MARKO JOVANOVIC")
    ours = run_line("MARKO JOVANOVIC")
    ours = RunLine(
        key="MARKO JOVANOVIC", name="Йованович Марко", totals=ours.totals,
        hours=ours.hours, insured_hours=ours.insured_hours,
        base_rate=ours.base_rate, coefficient=ours.coefficient, meal=ours.meal,
    )
    result = compare([theirs], {"MARKO JOVANOVIC": ours})

    assert only(result.lines).matched, "строка обязана сопоставиться по ключу"


# --- причина расхождения -----------------------------------------------------


def kinds(line) -> set[str]:
    return {cause.kind for cause in line.causes}


def test_hours_difference_is_named_as_a_cause():
    result = compare(
        [file_row(net="1000.00", hours={"regular": D("176")})],
        {"MARKO JOVANOVIC": run_line(net="1200.00", hours={"regular": D("160")})},
    )

    line = only(result.lines)
    assert "hours" in kinds(line)
    cause = next(c for c in line.causes if c.kind == "hours")
    assert cause.code == "regular"
    assert cause.expected == D("176") and cause.actual == D("160")


def test_equal_hours_are_not_named_as_a_cause():
    result = compare(
        [file_row(net="1000.00")], {"MARKO JOVANOVIC": run_line(net="1200.00")}
    )
    assert "hours" not in kinds(only(result.lines))


def test_rate_and_coefficient_differences_are_named():
    result = compare(
        [file_row(net="1000.00", rate="371", coefficient="1")],
        {"MARKO JOVANOVIC": run_line(net="1200.00", rate="400", coefficient="1.2")},
    )
    assert {"rate", "coefficient"} <= kinds(only(result.lines))


def test_hand_written_meal_bonus_is_named():
    """Надбавку бухгалтер иногда ставит руками — это названная причина."""
    result = compare(
        [file_row(net="1000.00", meal="1500")],
        {"MARKO JOVANOVIC": run_line(net="1200.00", meal="1363.64")},
    )
    assert "meal" in kinds(only(result.lines))


def test_a_matching_row_gets_no_causes():
    """Причины — объяснение расхождения. У сошедшейся строки их нет."""
    result = compare(
        [file_row(hours={"regular": D("176")})],
        {"MARKO JOVANOVIC": run_line(hours={"regular": D("160")})},
    )
    assert only(result.lines).matched
    assert not only(result.lines).causes, "у сошедшейся строки причин быть не может"


def test_a_zero_hour_kind_missing_on_one_side_is_not_a_difference():
    """«Больничного нет» и «больничный ноль» — одно и то же число часов."""
    result = compare(
        [file_row(net="1000.00", hours={"regular": D("176"), "sick": D("0")})],
        {"MARKO JOVANOVIC": run_line(net="1200.00", hours={"regular": D("176")})},
    )
    assert "hours" not in kinds(only(result.lines))


# --- сводка ------------------------------------------------------------------


def test_findings_of_the_parser_are_carried_into_the_report():
    """Не прочитанное файла — часть ответа: сверка неполного файла не «сошлась»."""
    finding = Finding("sheet", "Лист 9", "лист не входит в формат PLATA")
    result = compare([file_row()], {"MARKO JOVANOVIC": run_line()}, findings=[finding])

    assert result.findings == [finding]
    assert not result.clean, "файл прочитан не весь — сверка не может быть чистой"


def test_a_clean_reconciliation_says_so():
    result = compare([file_row()], {"MARKO JOVANOVIC": run_line()})
    assert result.clean


def test_lines_are_sorted_worst_first():
    """Читают сверху: сначала то, что разошлось сильнее всего."""
    result = compare(
        [file_row("A", net="1000"), file_row("B", net="1000"), file_row("C", net="1000")],
        {
            "A": run_line("A", net="1000"),
            "B": run_line("B", net="9000"),
            "C": run_line("C", net="1000.10"),
        },
    )
    assert [line.name for line in result.lines] == ["B", "C", "A"]


def test_totals_are_summed_only_over_compared_rows():
    """Итог сверки — по сравнимым строкам. Несравнимое в него не течёт.

    Две разные причины несравнимости, и обе обязаны выпасть из итога: строки
    вовсе нет в расчёте (B) и нето не прочитано из файла (C). Иначе итог
    складывает одну сторону там, где второй нет, и расходится сам с собой.
    """
    result = compare(
        [file_row("A", net="1000"), file_row("B", net="7000"), file_row("C", net=None)],
        {"A": run_line("A", net="1200"), "C": run_line("C", net="5000")},
    )
    assert result.total_expected == D("1000")
    assert result.total_actual == D("1200")
    assert result.total_diff == D("200")


@pytest.mark.parametrize("code", ["net", "gross", "contributions", "total_cost"])
def test_all_four_fields_of_the_table_are_compared(code):
    result = compare([file_row()], {"MARKO JOVANOVIC": run_line()})
    assert amount_of(only(result.lines), code) is not None
