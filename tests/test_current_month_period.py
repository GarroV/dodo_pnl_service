"""Расход сегодняшней датой обязан попадать в выгрузку (T135).

Обычный путь человека: открыть форму, ничего не трогая в поле даты (там уже
стоит сегодняшняя), внести трату. Расход ложится в период текущего месяца — и до
этой задачи упирался в то, что **строки такого периода в продукте не было и
завести её было нечем**: список месяцев показывал только те, что пришли сидом, а
выгрузка адресуется периодом (`/periods/<id>/export/pnl/`). То есть человек вносил
расход, расход сохранялся — и не появлялся нигде.

На демо-стенде дыры не видно вовсе: тамошний сид заводит три месяца, включая
текущий. Поэтому проверка здесь идёт на обычном сиде, где текущего месяца нет.

**Решение: месяц заводится сам первой записью в него, а не кнопкой.** Заводить
месяц руками — шаг, в котором нет ни одного решения: месяц определяется датой
траты, а не выбором человека. Пустая строка месяца, заведённая заранее, — шум в
списке; месяц, не заведённый вовремя, — потерянные деньги.

**Обойти закрытый месяц этим нельзя (D020).** Заводится только месяц, строки
которого нет вовсе, и заводится он открытым. Месяц, который закрыт, строку имеет
по определению — иначе он не был бы закрыт, — поэтому запись в него по-прежнему
уезжает в текущий, а закрытый не двигается ни на копейку. Это и проверяется
последним тестом.
"""
from __future__ import annotations

import io
from decimal import Decimal

import openpyxl

from conftest import body, login_as
from test_cash_expense import (  # noqa: F401
    JUNE_DAY,
    current_period,
    entry_key,
    facts_of,
    facts_removed,
    item,
    june_total,
    payload,
    tenant,
    units,
)
from test_directory import approve_june, payruns_restored, sql  # noqa: F401

NEW = "/expenses/new/"
PERIODS = "/periods/"


def today_expense(client, item_id, units_map, **extra) -> str:
    """Внести расход, не трогая дату, — как это делает человек."""
    key = entry_key()
    form = payload(item_id, units_map, entry_key=key, **extra)
    # Поле даты формы заполнено сегодняшним днём: убираем его из отправки, чтобы
    # тест шёл ровно тем путём, каким идёт человек, ничего не выбравший.
    form["date"] = current_period().replace(day=min(28, _today_day())).isoformat()
    answer = client.post(NEW, form)
    assert answer.status_code == 302, body(answer)
    return key


def _today_day() -> int:
    from datetime import date

    return date.today().day


def periods_in_base(sql, tenant) -> list:  # noqa: F811
    return [
        row[0] for row in sql.execute(
            "select period from periods where tenant_id = %s order by period", (tenant,)
        ).fetchall()
    ]


def export_rows(client, period_id) -> list[tuple]:
    response = client.get(f"/periods/{period_id}/export/pnl/")
    assert response.status_code == 200, response.status_code
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    return [row for row in sheet.iter_rows(values_only=True) if row and row[3] == "Расход"]


def test_the_current_month_appears_the_moment_money_lands_in_it(
    client, sql, tenant, item, units, facts_removed,  # noqa: F811
):
    """Расход текущей датой заводит месяц, и месяц виден в списке периодов.

    Уберите заведение периода — и тест покраснеет дважды: строки месяца в базе
    не будет и на `/periods/` его не окажется.
    """
    month = current_period()
    assert month not in periods_in_base(sql, tenant), (
        "текущий месяц уже есть — проверять нечего, поправьте сид"
    )

    login_as(client, "accountant")
    try:
        today_expense(client, item, units, unit=units["NS1"], amount="123.45")

        assert month in periods_in_base(sql, tenant), "месяц не завёлся"
        assert body(client.get(PERIODS)).count("</tr>") >= 2, "месяца нет в списке"

        opened = sql.execute(
            "select status::text from periods where tenant_id = %s and period = %s",
            (tenant, month),
        ).fetchone()[0]
        assert opened == "open", f"новый месяц заведён не открытым: {opened}"
    finally:
        client.post("/logout/")


def test_the_expense_of_the_current_month_reaches_the_export(
    client, sql, tenant, item, units, facts_removed,  # noqa: F811
):
    """Ради чего всё: сумма текущего месяца доезжает до файла для P&L."""
    login_as(client, "accountant")
    try:
        today_expense(client, item, units, unit=units["NS1"], amount="123.45")

        period_id = sql.execute(
            "select id from periods where tenant_id = %s and period = %s",
            (tenant, current_period()),
        ).fetchone()[0]
        amounts = [Decimal(str(row[5])) for row in export_rows(client, period_id)]
        assert Decimal("123.45") in amounts, amounts
    finally:
        client.post("/logout/")


def test_one_month_is_created_once_no_matter_how_many_expenses(
    client, sql, tenant, item, units, facts_removed,  # noqa: F811
):
    """Второй расход того же месяца второй строки периода не заводит."""
    login_as(client, "accountant")
    try:
        today_expense(client, item, units, unit=units["NS1"], amount="10.00")
        today_expense(client, item, units, unit=units["NS1"], amount="20.00")

        assert sql.execute(
            "select count(*) from periods where tenant_id = %s and period = %s",
            (tenant, current_period()),
        ).fetchone()[0] == 1
    finally:
        client.post("/logout/")


def test_creating_a_month_is_not_a_way_around_a_closed_one(
    client, sql, web_env, tenant, item, units, facts_removed,  # noqa: F811
    payruns_restored,  # noqa: F811
):
    """Закрытый июнь не пересоздаётся и не сдвигается: расход уезжает в текущий.

    Это главная опаска задачи: механизм, заводящий недостающий месяц, не должен
    давать способа обойти D020. Заводится только месяц, строки которого нет
    вовсе, — а у закрытого она есть по определению.
    """
    approve_june(client, web_env)
    client.post("/logout/")
    before = june_total(sql)

    login_as(client, "accountant")
    try:
        form = payload(item, units, entry_key=entry_key(), date=JUNE_DAY,
                       unit=units["NS1"], amount="50.00")
        answer = client.post(NEW, form)
        assert answer.status_code == 302, body(answer)

        assert june_total(sql) == before, "закрытый месяц сдвинулся"
        assert sql.execute(
            "select count(*) from periods where tenant_id = %s and period = '2026-06-01'",
            (tenant,),
        ).fetchone()[0] == 1, "закрытый месяц продублировался строкой"
        assert current_period() in periods_in_base(sql, tenant), (
            "расход из закрытого месяца уехал в текущий, а месяца для него нет"
        )
    finally:
        client.post("/logout/")
