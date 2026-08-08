"""
Импорт таблицы партнёра в табель (T020) и отчёт разбора (T021).

Три уровня, и каждый ловит своё:

1. **Разбор файла** — без базы. Здесь проверяется не то, что эталон читается
   (это делает регрессия движка), а то, что **испорченный файл не молчит**:
   чужой лист, переименованная колонка, текст вместо числа и непронумерованная
   строка обязаны попасть в находки, а не пропасть.
2. **Загрузка в продукт** — на живой базе. Эталонная таблица даёт корректный
   табель, суммы расчёта после неё те же, а повторная загрузка не меняет
   **ничего**: ни строк, ни чисел, ни идентификаторов дней.
3. **Проверки данных** — часов больше нормы, сотрудник без часов, часы у
   уволенного: подсказки, ради которых отчёт и нужен.
"""
from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal

import pytest

from conftest import PLATA_SAMPLE, wipe_payruns

JUNE = date(2026, 6, 1)


# =============================================================================
# 1. Разбор файла: находки вместо молчания
# =============================================================================


@pytest.fixture
def sample_copy(tmp_path):
    """Копия эталона, которую можно портить."""
    def make(name: str = "broken.xlsx"):
        target = tmp_path / name
        shutil.copy(PLATA_SAMPLE, target)
        return target
    return make


def _read(path):
    from payroll.importers import read_plata_file

    return read_plata_file(path)


def test_clean_reference_has_no_findings():
    """Эталон обязан читаться начисто: иначе находки перестанут что-то значить."""
    parsed = _read(PLATA_SAMPLE)
    assert len(parsed.rows) == 32
    assert parsed.findings == [], [f.text for f in parsed.findings]


def test_reader_keeps_reading_the_same_rows_as_before():
    """Старый вход не должен поехать: на нём стоит вся регрессия движка."""
    from payroll.importers import read_plata, read_plata_file

    assert [row.name for row in read_plata(PLATA_SAMPLE)] == [
        row.name for row in read_plata_file(PLATA_SAMPLE).rows
    ]


def test_unknown_sheet_is_reported(sample_copy):
    import openpyxl

    path = sample_copy()
    wb = openpyxl.load_workbook(path)
    wb.create_sheet("Заметки бухгалтера")
    wb.save(path)

    parsed = _read(path)
    assert len(parsed.rows) == 32, "чужой лист не должен уносить разобранные строки"
    assert [f.where for f in parsed.findings if f.kind == "sheet"] == [
        "Заметки бухгалтера"
    ]


def test_missing_sheet_of_the_format_is_reported(sample_copy):
    """Листа формата нет — значит его людей в загрузке нет. Это надо сказать."""
    import openpyxl

    path = sample_copy()
    wb = openpyxl.load_workbook(path)
    del wb["NS 2 Dunavska "]
    wb.save(path)

    parsed = _read(path)
    assert len(parsed.rows) == 28
    missing = [f for f in parsed.findings if f.kind == "sheet"]
    assert len(missing) == 1
    assert "NS 2 Dunavska" in missing[0].where


def test_renamed_column_is_reported(sample_copy):
    import openpyxl

    path = sample_copy()
    wb = openpyxl.load_workbook(path)
    ws = wb["NS 1 Bulevar "]
    for col in range(1, ws.max_column + 1):
        if str(ws.cell(1, col).value or "").strip().upper().startswith("SATI RADA"):
            ws.cell(1, col).value = "SATI (novo)"
            break
    else:  # pragma: no cover — эталон обязан содержать эту колонку
        pytest.fail("в эталоне нет колонки часов — тест проверял бы не то")
    wb.save(path)

    parsed = _read(path)
    columns = [f for f in parsed.findings if f.kind == "column"]
    assert columns, "переименованная колонка прошла молча"
    assert any("regular" in f.text or "SATI RADA" in f.text for f in columns)


def test_text_instead_of_number_is_reported_and_row_is_not_guessed(sample_copy):
    """Худший исход — принять «восемь» за ноль и посчитать зарплату по нему."""
    import openpyxl

    path = sample_copy()
    wb = openpyxl.load_workbook(path)
    ws = wb["NS 1 Bulevar "]
    for col in range(1, ws.max_column + 1):
        if str(ws.cell(1, col).value or "").strip().upper().startswith("SATI RADA"):
            ws.cell(2, col).value = "восемь"
            break
    wb.save(path)

    parsed = _read(path)
    assert len(parsed.rows) == 31, "строка с нечисловым значением не должна попасть в загрузку"
    values = [f for f in parsed.findings if f.kind == "value"]
    assert values and "восемь" in values[0].text


def test_unnumbered_row_with_a_name_is_reported(sample_copy):
    """Раньше такие строки исчезали молча — вместе с человеком."""
    import openpyxl

    path = sample_copy()
    wb = openpyxl.load_workbook(path)
    ws = wb["NS 1 Bulevar "]
    ws.cell(6, 2).value = "ZABORAVLJENI"
    ws.cell(6, 3).value = "RADNIK"
    wb.save(path)

    parsed = _read(path)
    assert len(parsed.rows) == 32
    rows = [f for f in parsed.findings if f.kind == "row"]
    assert rows and "ZABORAVLJENI" in rows[0].text


def test_rows_below_the_old_scan_limit_are_read(sample_copy):
    """Разбор ограничивался 18 строками на лист: 19-й человек пропадал молча."""
    import openpyxl

    path = sample_copy()
    wb = openpyxl.load_workbook(path)
    ws = wb["NS 1 Bulevar "]
    for line in range(6, 26):
        for col in range(1, ws.max_column + 1):
            ws.cell(line, col).value = ws.cell(2, col).value
        ws.cell(line, 1).value = line
        ws.cell(line, 2).value = f"DVADESET{line}"
    wb.save(path)

    parsed = _read(path)
    assert len(parsed.rows) == 52


# =============================================================================
# 2. Загрузка в продукт
# =============================================================================


@pytest.fixture
def period_restored(web_env):
    """Снимок табеля периода до теста и точный возврат к нему после.

    База `web_env` живёт весь прогон, а импорт переписывает её целиком —
    без возврата следующий тест считал бы уже не то, что задумано.
    """
    from core.models import Timesheet, TimesheetDay

    fields = [f.name for f in Timesheet._meta.concrete_fields]
    sheets = [
        {name: getattr(row, name) for name in fields}
        for row in Timesheet.objects.filter(period=JUNE)
    ]
    day_fields = [f.name for f in TimesheetDay._meta.concrete_fields]
    days = [
        {name: getattr(day, name) for name in day_fields}
        for day in TimesheetDay.objects.filter(timesheet__period=JUNE)
    ]
    yield
    TimesheetDay.objects.filter(timesheet__period=JUNE).delete()
    Timesheet.objects.filter(period=JUNE).delete()
    Timesheet.objects.bulk_create([Timesheet(**row) for row in sheets])
    TimesheetDay.objects.bulk_create([TimesheetDay(**day) for day in days])


def _tenant_id():
    from core.models import Tenant

    return Tenant.objects.get(code="rs-dev").id


def _import(path=PLATA_SAMPLE, **kw):
    from timesheets.importer import import_partner_table

    with open(path, "rb") as handle:
        return import_partner_table(handle, tenant_id=_tenant_id(), period=JUNE, **kw)


def test_import_of_reference_table_fills_the_timesheet(period_restored):
    """Строки табеля заводятся заново и повторяют числа файла."""
    from core.models import Timesheet
    from payroll.importers import read_plata

    Timesheet.objects.filter(period=JUNE).delete()

    result = _import()
    assert result.created == 32
    assert result.matched == 32
    assert result.unmatched_rows == []

    file_rows = {row.employee.ext_id: row for row in read_plata(PLATA_SAMPLE)}
    stored = Timesheet.objects.select_related("employee").filter(period=JUNE)
    assert stored.count() == 32
    for row in stored:
        source = file_rows[row.employee.external_id]
        assert row.insured_hours == source.timesheet.insured_hours
        for kind, hours in source.timesheet.hours.items():
            assert Decimal(str(row.hours.get(kind, 0))) == hours, (row.employee.external_id, kind)


def test_second_import_changes_nothing(period_restored):
    """Ни строк, ни чисел, ни идентификаторов дней (D023, критерий T020)."""
    from core.models import Timesheet, TimesheetDay

    Timesheet.objects.filter(period=JUNE).delete()
    _import()

    def snapshot():
        sheets = sorted(
            (str(r.id), str(r.employee_id), str(r.unit_id), str(r.hours),
             str(r.insured_hours), str(r.norm_hours), str(r.deduction),
             str(r.cash_payout), str(r.manual_correction), r.created_at.isoformat())
            for r in Timesheet.objects.filter(period=JUNE)
        )
        days = sorted(
            (str(d.id), str(d.timesheet_id), d.work_date.isoformat(), d.hour_type,
             str(d.hours), d.source, d.created_at.isoformat())
            for d in TimesheetDay.objects.filter(timesheet__period=JUNE)
        )
        return sheets, days

    before = snapshot()
    again = _import()

    assert again.created == 0
    assert again.updated == 0
    assert again.unchanged == 32
    assert snapshot() == before


def test_import_over_edited_row_keeps_days_in_sync(period_restored):
    """Строка уже подневная — импорт обязан переписать и дни, а не только итог.

    Иначе инвариант «итог равен сумме дней» рвётся ровно там, где табель уже
    правили руками, — и заметить это стало бы нечем.
    """
    from core.models import Timesheet
    from timesheets import store

    row = (
        Timesheet.objects.select_related("employee")
        .filter(period=JUNE, employee__external_id="VUK MILOSEVIC")
        .first()
    )
    store.set_cell(timesheet=row, hour_type="regular", hours=Decimal("11.00"))
    assert store.daily_totals(row)["regular"] == Decimal("11.00")

    result = _import()
    assert result.updated >= 1

    row.refresh_from_db()
    totals = store.daily_totals(row)
    assert totals["regular"] == Decimal(str(row.hours["regular"]))
    assert totals["regular"] != Decimal("11.00")


def test_import_does_not_move_the_calculation(web_env, period_restored):
    """Суммы после загрузки эталона — те же, что были приняты на сиде.

    Ориентир снят на приёмке: директор видит 60 строк ведомости на 1 951 806,13.
    Импорт пишет тот же файл, из которого собран сид, поэтому сдвинуться нечему —
    и если сдвинулось, то не в данных, а в записи.
    """
    from django.db.models import Sum

    from core.models import PayComponent
    from payrun.calc import calculate_period

    all_ledgers = ["official", "supplementary", "internal"]

    wipe_payruns(web_env)
    before = calculate_period(tenant_id=_tenant_id(), period=JUNE, visible_ledgers=all_ledgers)
    total_before = PayComponent.objects.filter(
        payslip__payrun_id=before.payrun_id
    ).aggregate(total=Sum("amount"))["total"]
    assert total_before == Decimal("1951806.13")

    _import()

    wipe_payruns(web_env)
    after = calculate_period(tenant_id=_tenant_id(), period=JUNE, visible_ledgers=all_ledgers)
    assert PayComponent.objects.filter(
        payslip__payrun_id=after.payrun_id
    ).aggregate(total=Sum("amount"))["total"] == Decimal("1951806.13")


def test_unknown_employee_lands_in_unmatched_rows(period_restored):
    """Человека нет в справочнике — строка не теряется, а называется."""
    from core.models import Employee

    Employee.objects.filter(external_id="VUK MILOSEVIC").delete()

    result = _import()
    assert [row.name for row in result.unmatched_rows] == ["VUK MILOSEVIC"]
    assert result.matched == 31


# =============================================================================
# 3. Проверки данных: подсказки о подозрительном (T021)
# =============================================================================


def _texts(result, kind: str) -> list[str]:
    return [note.text for note in result.warnings if note.kind == kind]


def test_hours_above_norm_are_flagged(period_restored, sample_copy):
    import openpyxl

    path = sample_copy()
    wb = openpyxl.load_workbook(path)
    ws = wb["NS 1 Bulevar "]
    for col in range(1, ws.max_column + 1):
        if str(ws.cell(1, col).value or "").strip().upper().startswith("SATI RADA"):
            ws.cell(2, col).value = 400
            break
    wb.save(path)

    result = _import(path)
    assert any("VUK MILOSEVIC" in text for text in _texts(result, "over_norm"))


def test_employee_without_hours_is_flagged(period_restored, sample_copy):
    import openpyxl

    path = sample_copy()
    wb = openpyxl.load_workbook(path)
    ws = wb["NS 1 Bulevar "]
    for col in range(1, ws.max_column + 1):
        title = str(ws.cell(1, col).value or "").strip().upper()
        if title.startswith(("SATI RADA", "SAT NA RAD", "GODISNJI", "SATI BOLOVANJE")):
            ws.cell(2, col).value = 0
    wb.save(path)

    result = _import(path)
    assert any("VUK MILOSEVIC" in text for text in _texts(result, "no_hours"))


def test_hours_of_a_dismissed_employee_are_flagged(period_restored):
    from core.models import Employee

    Employee.objects.filter(external_id="VUK MILOSEVIC").update(
        dismissed_at=date(2026, 5, 31)
    )
    try:
        result = _import()
        assert any("VUK MILOSEVIC" in text for text in _texts(result, "dismissed"))
    finally:
        Employee.objects.filter(external_id="VUK MILOSEVIC").update(dismissed_at=None)


def test_clean_reference_import_has_no_data_warnings(period_restored):
    """Иначе предупреждения станут фоном, который перестают читать."""
    result = _import()
    assert result.warnings == [], [note.text for note in result.warnings]
