"""Счета поставщиков в файле «Строки для P&L» — рядом с зарплатой и кассой (T153).

Строка счёта едет в выгрузку тем же путём, что и расход из кассы: через
`collect_expenses` и условие `EXPENSE_LINES` в `reports/export.py`. Путь этот ни
разу не проверен — а «по коду должно работать» проверкой не считается. Условие
исключает переводы и зарплату по `kind`/`source`, и ошибиться в нём можно один
раз и не заметить: сумма не пропадает с шумом, она либо тихо не приезжает, либо
тихо приезжает дважды.

Пять фактов ниже, и каждый — про деньги, а не про код:

1. Счёт стоит в файле рядом с зарплатой и кассой, тем же форматом строки.
2. Платёж по счёту (`kind = 'transfer'`) не удваивает расход — ни новой строкой,
   ни ростом суммы существующей.
3. Счёт без статьи остаётся в файле под служебной «Не разобрано», а не пропадает.
4. Оплата без счёта признаётся расходом датой денег.
5. Управляющий видит в файле только свою точку — срез делает база (D014), а не
   выборка в коде отчёта.
"""
from __future__ import annotations

import io
from decimal import Decimal

import openpyxl
import pytest

from conftest import body, login_as
from test_cash_expense import payload as cash_payload
from test_directory import payruns_restored, sql  # noqa: F401
from test_supplier_invoices import (  # noqa: F401
    JULY_DAY,
    NEW,
    counterparty,
    invoice_form,
    invoices_removed,
    item,
    key,
    lines,
    tenant,
    units,
)

D = Decimal
JUNE = "2026-06-01"
JULY = "2026-07-01"
CASH_NEW = "/expenses/new/"
PAYMENTS_NEW = "/payments/new/"


@pytest.fixture
def cash_item(sql, tenant):  # noqa: F811
    """Вторая статья — специально для расхода из кассы, отдельная от статьи счёта.

    Разным источникам — разные статьи. Иначе строка кассы и строка счёта легли
    бы в одну и ту же ячейку (`spent` в `reports.export.pnl` складывает суммы по
    ключу «статья, точка, регистр, название»), и по файлу нельзя было бы понять,
    доехал ли счёт вообще, или это целиком касса.
    """
    from psycopg.types.json import Jsonb

    line = sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    row = sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 'cash-test-153', %s, %s, '2020-01-01') returning id""",
        (tenant, Jsonb({"ru": "Вода", "en": "Water", "sr-latn": "Voda"}), line),
    ).fetchone()[0]
    yield str(row)
    sql.execute("delete from facts where expense_item_id = %s", (row,))
    sql.execute("delete from expense_items where id = %s", (row,))


def period_page(sql, tenant, period: str) -> str:  # noqa: F811
    """Адрес периода по значению `period`, а не первой ссылкой со списка.

    Со списка приезжает произвольный по счёту месяц, и тест зависел бы от того,
    что уже успели насчитать соседи в общей базе стенда (`web_env` на весь прогон).
    """
    period_id = sql.execute(
        "select id from periods where tenant_id = %s and period = %s", (tenant, period)
    ).fetchone()[0]
    return f"/periods/{period_id}/"


def export_url(sql, tenant, period: str) -> str:  # noqa: F811
    return period_page(sql, tenant, period) + "export/pnl/"


def pnl_rows(client, url) -> list[tuple]:
    """Строки готового файла периода — тем же путём, что качает бухгалтер."""
    response = client.get(url)
    assert response.status_code == 200, response.status_code
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    return [
        row for row in sheet.iter_rows(values_only=True)
        if any(value is not None for value in row)
    ]


def expense_rows(rows: list[tuple]) -> list[tuple]:
    return [row for row in rows if row and row[3] == "Расход"]


def money_of(rows: list[tuple]) -> list[Decimal]:
    return [Decimal(str(row[5])) for row in rows]


def article_total(rows: list[tuple], title: str) -> Decimal:
    """Сумма расходных строк с данным названием позиции — для проверки удвоения."""
    return sum(money_of([row for row in expense_rows(rows) if row[4] == title]), D("0"))


# --- 1. счёт рядом с зарплатой и кассой -----------------------------------


def test_a_supplier_invoice_lands_next_to_payroll_and_cash_in_one_file(
    client, sql, tenant, cash_item, counterparty, item, units,  # noqa: F811
    invoices_removed, payruns_restored,  # noqa: F811
):
    """Три источника денег — ведомость, касса, счёт — в одном файле одного вида.

    Строка кассы уже доезжает до файла (`test_export_expenses`), строка счёта —
    нет нигде. Если она перестанет доезжать, дыру в P&L найдут только руками,
    сверяя файл с таблицей партнёра, — то есть узнают о ней последними.
    """
    login_as(client, "director")
    page = period_page(sql, tenant, JUNE)
    assert client.post(page + "calculate/", {"inline": "1"}, follow=True).status_code == 200
    client.post("/logout/")

    login_as(client, "accountant")
    assert client.post(
        CASH_NEW, cash_payload(cash_item, units, unit=units["NS1"], amount="111.11"),
    ).status_code == 302
    assert client.post(
        NEW, invoice_form(counterparty, units, item=item, unit=units["NS2"], amount="222.22"),
    ).status_code == 302

    rows = pnl_rows(client, export_url(sql, tenant, JUNE))
    kinds = {row[3] for row in rows if row[3]}
    assert "Начисление" in kinds, f"зарплата пропала из файла: {rows}"
    assert "Расход" in kinds, f"расходов в файле нет вовсе: {rows}"

    expense = expense_rows(rows)
    assert sorted(money_of(expense)) == [D("111.11"), D("222.22")], expense
    titles = {row[4] for row in expense}
    assert titles == {"Вода", "Электричество"}, f"касса и счёт слились в одну строку: {expense}"

    accrual = [row for row in rows if row[3] == "Начисление"]
    assert accrual, "зарплата пропала из файла, как только в нём появился счёт"
    # Ширина строки одна: загрузчик P&L читает файл одним разбором.
    assert len(expense[0]) == len(accrual[0])


# --- 2. платёж не удваивает расход ------------------------------------------


def test_paying_an_invoice_does_not_add_a_second_expense_row(
    client, sql, tenant, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Платёж — перевод (`kind = 'transfer'`), расход уже признан счётом.

    Мало проверить, что строки платежа нет: удвоение — это ещё и рост суммы
    существующей строки на ту же величину, без единой новой строки. Проверяются
    обе формы удвоения, а не одна.
    """
    login_as(client, "accountant")
    assert client.post(
        NEW, invoice_form(counterparty, units, item=item, amount="24000.00"),
    ).status_code == 302
    document = lines(sql)[0][7]

    url = export_url(sql, tenant, JUNE)
    before = pnl_rows(client, url)
    assert article_total(before, "Электричество") == D("24000.00"), (
        f"счёт не доехал до файла раньше платежа: {before}"
    )

    paid = client.post(f"/invoices/{document}/pay/", {
        "date": "2026-06-20", "amount": "24000.00", "entry_key": key(),
    })
    assert paid.status_code == 302, body(paid)

    after = pnl_rows(client, url)
    assert len(expense_rows(after)) == len(expense_rows(before)), (
        f"платёж добавил новую расходную строку: {after}"
    )
    assert article_total(after, "Электричество") == D("24000.00"), (
        "оплата счёта удвоила сумму статьи в файле"
    )


# --- 3. счёт без статьи виден числом ----------------------------------------


def test_an_invoice_without_an_expense_item_stays_visible_as_unclassified(
    client, sql, tenant, counterparty, units, invoices_removed,  # noqa: F811
):
    """Счёт без статьи получает служебную «Не разобрано» и остаётся в файле.

    Молча пропавшая сумма — дыра в P&L хуже видимой: без этой проверки прогон
    остался бы зелёным и в том случае, если `pnl_lines` вдруг соединится со
    статьёй внутренним соединением и потеряет строку без неё вовсе.
    """
    login_as(client, "accountant")
    answer = client.post(NEW, invoice_form(counterparty, units, amount="999.99"))
    assert answer.status_code == 302, body(answer)

    rows = pnl_rows(client, export_url(sql, tenant, JUNE))
    unclassified = [row for row in expense_rows(rows) if row[0] == "Не разобрано"]
    assert unclassified, f"строка без статьи пропала из файла: {rows}"
    assert money_of(unclassified) == [D("999.99")], unclassified


# --- 4. оплата без счёта -----------------------------------------------------


def test_an_advance_payment_without_an_invoice_reaches_the_file(
    client, sql, tenant, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Оплата без счёта признаётся расходом датой денег — и обязана доехать до
    файла, а не остаться только строкой в базе, которую никто не выгружает."""
    login_as(client, "accountant")
    answer = client.post(PAYMENTS_NEW, {
        "date": JULY_DAY, "counterparty": counterparty, "item": item,
        "unit": units["NS1"], "ledger": "official", "amount": "3200.00",
        "note": "лампочки", "entry_key": key(),
    })
    assert answer.status_code == 302, body(answer)

    rows = pnl_rows(client, export_url(sql, tenant, JULY))
    expense = expense_rows(rows)
    assert money_of(expense) == [D("3200.00")], f"оплата без счёта не доехала до файла: {rows}"


# --- 5. срез по точке ---------------------------------------------------------


@pytest.fixture
def three_invoices(client, counterparty, item, units, invoices_removed):  # noqa: F811
    """Три июньских счёта на трёх точках, внесённые бухгалтером с экрана."""
    login_as(client, "accountant")
    for code, amount in (("NS1", "100.00"), ("NS2", "200.00"), ("BG1", "300.00")):
        answer = client.post(NEW, invoice_form(
            counterparty, units, item=item, unit=units[code], amount=amount,
            number=f"EPS-{code}",
        ))
        assert answer.status_code == 302, body(answer)
    client.post("/logout/")


def test_the_manager_sees_only_his_own_unit_in_the_pnl_file(
    client, sql, tenant, three_invoices,  # noqa: F811
):
    """Файл — срез смотрящего, и срез делает база (D014), не выборка кода.

    Сломайте `unit_visibility` на `facts` — в файле управляющего появятся чужие
    счета и чужие суммы, и он соберёт P&L по данным, которых ему не показывали.
    """
    url = export_url(sql, tenant, JUNE)

    login_as(client, "accountant")
    whole = expense_rows(pnl_rows(client, url))
    assert sorted(money_of(whole)) == [D("100"), D("200"), D("300")], whole
    client.post("/logout/")

    login_as(client, "manager")
    mine = expense_rows(pnl_rows(client, url))
    assert [row[1] for row in mine] == ["NS1"], mine
    assert money_of(mine) == [D("100")], mine
