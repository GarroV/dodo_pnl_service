"""
Что показывает наполненная демо-база: продукт целиком, а не пустые экраны.

Definition of Done блока говорит прямо: демо готово, когда **в нём работают и
отчёт расхождений, и сверка**. Это не про то, что код отчёта не падает, — это
про то, что на демо-данных ему есть что показать. Отчёт расхождений без второго
месяца и сверка без файла честно пусты, и такое демо показывать нельзя.

Проверки идут на настоящей базе, наполненной той же командой, которую запускает
человек. Отчёты считаются в подпроцессе через `manage.py shell`: настройки
Django читаются один раз на процесс, а в прогоне тестов уже поднята другая база.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from conftest import MANAGE_PY, temp_database

CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def manage(dsn: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "DEMO_DATABASE_URL": dsn,
        "SECRET_KEY": "test-only-not-a-secret",
        "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    return subprocess.run(
        [sys.executable, str(MANAGE_PY), *args],
        env=env, capture_output=True, text=True, check=True,
    )


@pytest.fixture(scope="module")
def demo_db():
    with temp_database("content") as dsn:
        manage(dsn, "seed_demo")
        yield dsn


@pytest.fixture
def conn(demo_db):
    psycopg = pytest.importorskip("psycopg")

    from core.db_types import register_enum_types

    with psycopg.connect(demo_db) as c:
        register_enum_types(c)
        yield c


def report(dsn: str, script: str) -> dict:
    """Посчитать что-нибудь внутри Django и вернуть результат словарём.

    Через `manage.py shell`, потому что отчёты живут в ORM, а второй базы в одном
    процессе Django не бывает. Печатается одна строка JSON — её и читаем.
    """
    out = manage(dsn, "shell", "-c", script).stdout
    return json.loads(out.strip().splitlines()[-1])


# --- организация ---------------------------------------------------------------


def test_two_legal_entities_three_units_thirty_people(conn):
    tenant = conn.execute("select id, title from tenants").fetchall()
    assert len(tenant) == 1, "в демо-базе должен быть ровно один партнёр"

    assert conn.execute("select count(*) from legal_entities").fetchone()[0] == 2
    units = [
        row[0]
        for row in conn.execute("select code from units order by code").fetchall()
    ]
    assert units == ["BG1", "NS1", "NS2"]
    # Обе точки Нови-Сада — на одном юрлице, Белград — на другом: два юрлица без
    # разделения точек не показывают ничего.
    per_entity = conn.execute(
        "select legal_entity_id, count(*) from units group by 1 order by 2"
    ).fetchall()
    assert [row[1] for row in per_entity] == [1, 2]

    assert conn.execute("select count(*) from employees").fetchone()[0] == 30


def test_three_months_two_of_them_closed(conn):
    periods = conn.execute(
        "select period, status from periods order by period"
    ).fetchall()
    assert len(periods) == 3
    assert [str(row[1]) for row in periods] == ["closed", "closed", "open"]

    payruns = conn.execute(
        "select period, status from payruns order by period"
    ).fetchall()
    assert [str(row[1]) for row in payruns] == ["approved", "approved", "calculated"]


def test_every_ledger_and_every_scheme_is_represented(conn):
    ledgers = {
        row[0]
        for row in conn.execute("select distinct ledger from pay_components").fetchall()
    }
    assert ledgers == {"official", "supplementary", "internal"}

    schemes = {
        row[0]
        for row in conn.execute(
            """select distinct coalesce(t.scheme, g.scheme)
                 from employment_terms t join employee_groups g on g.id = t.group_id"""
        ).fetchall()
    }
    assert {"standard", "half_time", "half_time_min_base", "temporary"} <= schemes


def test_employment_terms_are_versioned(conn):
    """У кого-то две версии условий найма: версионирование должно быть видно."""
    versions = conn.execute(
        """select employee_id, count(*) from employment_terms
            group by 1 having count(*) > 1"""
    ).fetchall()
    assert versions, "ни у кого нет второй версии условий найма"


def test_manual_correction_has_an_author_and_a_reason(conn):
    corrections = conn.execute(
        """select manual_correction, correction_reason, corrected_by
             from timesheets where manual_correction is not null"""
    ).fetchall()
    assert corrections
    for amount, reason, author in corrections:
        assert amount is not None and reason and author, "правка без следа (D025)"


# --- английский ----------------------------------------------------------------


def test_nothing_a_visitor_sees_is_written_in_russian(conn):
    """Демо всегда англоязычное — во всём, что лежит в базе строкой.

    Подписи компонентов выплаты сюда **не входят намеренно** (T092). В
    `pay_components.title` лежит подпись, замороженная расчётом на языке правил;
    посетителю она не показывается — колонка называется по правилам на языке
    страницы. Проверять здесь русское слово значило бы требовать от записи в
    базе того, чем она не является, и это требование однажды починили бы, сломав
    объяснение закрытого месяца. Что видит посетитель, проверяет тест ниже.
    """
    seen: list[str] = []
    for sql in (
        "select title from tenants",
        "select title from legal_entities",
        "select title from units",
        "select title from employee_groups",
        "select title from roles",
        "select first_name from employees",
        "select last_name from employees",
        "select correction_reason from timesheets where correction_reason is not null",
    ):
        seen += [row[0] for row in conn.execute(sql).fetchall() if row[0]]

    russian = sorted({text for text in seen if CYRILLIC.search(text)})
    assert not russian, f"по-русски в демо: {russian}"


def test_the_columns_a_visitor_sees_are_english(demo_db):
    """Колонки ведомости демо названы по-английски — тем же путём, что у партнёра.

    Спрашивается ровно то, что подставит страница: подписи компонентов на языке
    демо (`UI_LANGUAGE=en`). До T092 демо держало свой список английских подписей
    и выглядело англоязычным даже тогда, когда продукт показывал колонки
    по-русски. Список убран, и эта проверка — то, что стоит на его месте.
    """
    result = report(demo_db, """
import json
from datetime import date
from django.utils import translation
from core.models import Tenant
from web.labels import component_titles
t = Tenant.objects.get(code="demo")
with translation.override("en"):
    titles = component_titles(t.id, t.country_code, date(2026, 6, 1))
print(json.dumps(titles, ensure_ascii=False))
""")
    assert result, "подписей компонентов нет вовсе — колонки останутся без названий"
    russian = sorted(v for v in result.values() if CYRILLIC.search(v))
    assert not russian, f"колонки демо по-русски: {russian}"
    assert result.get("hours.regular") == "Worked hours", result.get("hours.regular")


# --- отчёты, ради которых стенд и наполняли ------------------------------------


def test_variance_report_has_something_to_show(demo_db):
    """Отчёт расхождений содержателен в обоих переходах, а не «сравнивать нечего».

    Ровно поэтому в демо три месяца, а не два: один переход показал бы одну
    пару, и пустой отчёт на второй паре списали бы на «так и должно быть».
    """
    result = report(demo_db, """
import json
from datetime import date
from core.models import Tenant
from reports.variance import build_variance
t = Tenant.objects.get(code="demo")
out = {}
for label, p in (("july", date(2026, 7, 1)), ("august", date(2026, 8, 1))):
    r = build_variance(t.id, p)
    out[label] = {
        "lines": len(r.lines), "compared": r.compared,
        "employees": r.employees, "nothing": r.nothing_to_compare,
    }
print(json.dumps(out))
""")
    for label, body in result.items():
        assert not body["nothing"], f"{label}: сравнивать не с чем"
        assert body["compared"] > 0
        assert body["lines"] >= 3, f"{label}: в отчёте почти пусто ({body['lines']})"
        assert body["employees"] >= 2, f"{label}: отклонения у одного человека"


def test_reconciliation_shows_all_of_its_states(demo_db):
    """Сверка на демо-файле показывает и совпадение, и все три вида расхождения.

    Файл собирается из самих демо-данных и несёт нарочные расхождения (см.
    `demo.table`). Проверяется не «сверка не падает», а что посетитель видит на
    экране все её состояния: сошлось, разошлось с объяснением, копейки, и люди,
    которых нет с одной из сторон.
    """
    result = report(demo_db, """
import io, json
from datetime import date
from core.models import Tenant
from demo.accountant_table import build_accountant_table
from demo.table import accountant_rows
from reports.reconcile import reconcile
period = date(2026, 6, 1)
book = build_accountant_table(accountant_rows(period))
t = Tenant.objects.get(code="demo")
r = reconcile(io.BytesIO(book), tenant_id=t.id, period=period)
print(json.dumps({
    "matched": sum(1 for line in r.lines if line.matched),
    "off": sum(1 for line in r.lines
               if line.compared and not line.matched and not line.rounding_only),
    "rounding": sum(1 for line in r.lines if line.rounding_only),
    "only_in_file": len(r.only_in_file),
    "only_in_run": len(r.only_in_run),
    "findings": len(r.findings),
    "causes": sum(len(line.causes) for line in r.lines),
}))
""")
    assert result["matched"] >= 20, "почти ничего не сошлось — файл не из этих данных"
    assert result["off"] >= 1, "нет ни одного существенного расхождения"
    assert result["rounding"] >= 1, "нет расхождения-округления"
    assert result["only_in_file"] >= 1, "нет человека, оставшегося только в файле"
    assert result["only_in_run"] >= 1, "нет человека, которого нет в таблице"
    assert result["causes"] >= 1, "расхождение не объясняет себя входами"
    assert result["findings"] == 0, "демо-файл разобрался с находками — формат поехал"


def test_payroll_sheet_is_not_empty_for_a_closed_month(demo_db):
    """Ведомость закрытого месяца показывает строки и колонки, а не пустоту."""
    result = report(demo_db, """
import json
from datetime import date
from core.models import Tenant
from reports.sheet import build_slice
t = Tenant.objects.get(code="demo")
s = build_slice(t.id, date(2026, 6, 1))
print(json.dumps({
    "rows": len(s.sheet.rows),
    "columns": len(s.sheet.columns),
    "ledgers": sorted(cut.code for cut in s.cuts),
}))
""")
    assert result["rows"] >= 29
    assert result["columns"] >= 3
    assert len(result["ledgers"]) >= 3, "разрезов по регистрам меньше трёх"
