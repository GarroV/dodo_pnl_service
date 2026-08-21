"""Инбокс классификации: строка без статьи видна числом и разбирается (T152).

Ради чего экран существует. Трата, у которой не выбрана статья, опаснее
отсутствующей: она **уже** сидит в P&L, но не там, где её будут искать. Поэтому
проверяется не «список рисуется», а три утверждения:

1. неразобранная сумма **есть в P&L** и **видна числом** на экране — она не
   исчезает и не прячется;
2. разбор происходит прямо в списке и уводит строку из инбокса **в ту статью**,
   которую выбрали, не двигая ни суммы, ни даты;
3. разбор задним числом не переписывает закрытый месяц (D020): в открытом
   появляются сторно и разобранная запись, а закрытый остаётся прежним.

Проверки идут через экраны. Кому какие строки видны — отдельно и ролью
`app_user` (`tests/test_supplier_access.py`).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import body, login_as
from test_directory import approve_june, payruns_restored, sql  # noqa: F401
from test_supplier_invoices import (  # noqa: F401
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

INBOX = "/inbox/"


@pytest.fixture
def unclassified(client, counterparty, units, invoices_removed):  # noqa: F811
    """Счёт без статьи: ровно то, ради чего инбокс и заведён."""
    login_as(client, "accountant")
    answer = client.post(NEW, invoice_form(counterparty, units, item=""))
    assert answer.status_code == 302, body(answer)
    return answer


def facts(sql):  # noqa: F811
    return sql.execute(
        """select dedup_key, amount, period, i.code, e.code, unit_id::text
             from facts f
             join pnl_items i on i.id = f.pnl_item_id
             left join expense_items e on e.id = f.expense_item_id
            where f.dedup_key like 'manual:invoice:%%' and f.superseded_at is null
            order by f.dedup_key"""
    ).fetchall()


# --- видна числом -------------------------------------------------------------


def test_a_line_without_an_article_lands_in_the_inbox(client, sql, unclassified):  # noqa: F811
    """Счёт без статьи записан, стоит в инбоксе и виден суммой."""
    (row,) = facts(sql)
    assert row[3] == "unclassified"     # служебная строка P&L
    assert row[4] is None               # статьи у расхода нет
    assert row[1] == Decimal("24000.00")

    shown = body(client.get(INBOX))
    assert 'data-total="24000.00"' in shown
    assert "EPS Elektro" in shown       # опознать трату можно по контрагенту


def test_the_unclassified_amount_is_counted_in_pnl(client, sql, unclassified):  # noqa: F811
    """Неразобранная сумма считается в P&L по сети, а не пропадает молча."""
    shown = sql.execute(
        """select coalesce(sum(amount), 0) from pnl_by_network
            where period = '2026-06-01' and pnl_code = 'unclassified'"""
    ).fetchone()[0]
    assert shown == Decimal("24000.00")


def test_an_empty_inbox_says_so(client, invoices_removed):  # noqa: F811
    login_as(client, "accountant")
    shown = body(client.get(INBOX))
    assert "Инбокс пуст" in shown


# --- разбор -------------------------------------------------------------------


def test_the_line_is_classified_right_in_the_list(
    client, sql, item, units, unclassified,  # noqa: F811
):
    """Разбор назначает статью и уводит строку из инбокса, не двигая сумму."""
    fact_id = facts(sql)[0][0]
    row_id = sql.execute(
        "select id from facts where dedup_key = %s", (fact_id,)
    ).fetchone()[0]

    answer = client.post(f"/inbox/{row_id}/classify/", {
        "item": item, "unit": units["NS1"],
    })
    assert answer.status_code == 302, body(answer)

    active = facts(sql)
    assert len(active) == 1
    key_, amount, period, pnl, expense, unit_id = active[0]
    assert pnl == "food_cost"                 # статья назначена
    assert expense == "inv-test"
    assert amount == Decimal("24000.00")      # сумма не двинулась
    assert period.isoformat() == "2026-06-01"  # период учёта не двинулся
    assert unit_id == units["NS1"]

    # И из инбокса строка ушла.
    assert "Инбокс пуст" in body(client.get(INBOX))
    # А человеку сказано, что случилось: молчание читается как успех.
    assert "разобрана" in body(client.get(INBOX, {"done": "2026-06"}))


def test_the_unclassified_amount_leaves_the_service_line_after_the_sort(
    client, sql, item, units, unclassified,  # noqa: F811
):
    """После разбора деньги стоят в своей статье, а «Не разобрано» пусто."""
    row_id = sql.execute(
        "select id from facts where dedup_key like 'manual:invoice:%%'"
    ).fetchone()[0]
    client.post(f"/inbox/{row_id}/classify/", {"item": item, "unit": units["NS1"]})

    shown = dict(sql.execute(
        """select pnl_code, amount from pnl_by_network
            where period = '2026-06-01' and pnl_code in ('unclassified', 'food_cost')"""
    ).fetchall())
    assert shown.get("unclassified") is None
    assert shown["food_cost"] == Decimal("24000.00")


def test_classifying_without_an_article_leaves_the_line_in_place(
    client, sql, units, unclassified,  # noqa: F811
):
    """Разбор без статьи — отказ словами, и строка остаётся в списке."""
    row_id = sql.execute(
        "select id from facts where dedup_key like 'manual:invoice:%%'"
    ).fetchone()[0]
    answer = client.post(f"/inbox/{row_id}/classify/", {"item": "", "unit": units["NS1"]},
                         follow=True)
    assert answer.status_code == 200
    assert "не разобрана" in body(answer)
    assert facts(sql)[0][3] == "unclassified"


def test_classify_answers_only_to_post(client, sql, unclassified):  # noqa: F811
    """Разбор пишет деньги: по ссылке из истории браузера он случиться не должен."""
    row_id = sql.execute(
        "select id from facts where dedup_key like 'manual:invoice:%%'"
    ).fetchone()[0]
    assert client.get(f"/inbox/{row_id}/classify/").status_code == 405


def test_an_already_sorted_line_answers_404(
    client, sql, item, units, unclassified,  # noqa: F811
):
    """Разобранная строка и выдуманный номер отвечают одинаково (D023)."""
    row_id = sql.execute(
        "select id from facts where dedup_key like 'manual:invoice:%%'"
    ).fetchone()[0]
    client.post(f"/inbox/{row_id}/classify/", {"item": item, "unit": units["NS1"]})

    again = client.post(f"/inbox/{row_id}/classify/", {"item": item, "unit": units["NS1"]})
    nobody = client.post(
        "/inbox/00000000-0000-4000-8000-0000000000a1/classify/",
        {"item": item, "unit": units["NS1"]},
    )
    assert again.status_code == nobody.status_code == 404


# --- закрытый месяц -----------------------------------------------------------


def test_sorting_a_closed_month_does_not_rewrite_it(
    client, web_env, sql, item, units, unclassified, payruns_restored,  # noqa: F811
):
    """Разбор задним числом — сторно и разобранная строка в открытом (D020)."""
    row_id = sql.execute(
        "select id from facts where dedup_key like 'manual:invoice:%%'"
    ).fetchone()[0]

    approve_june(client, web_env)
    before = sql.execute(
        "select coalesce(sum(amount), 0) from facts "
        "where period = '2026-06-01' and superseded_at is null"
    ).fetchone()[0]

    login_as(client, "accountant")
    answer = client.post(f"/inbox/{row_id}/classify/", {
        "item": item, "unit": units["NS1"],
    }, follow=True)
    assert answer.status_code == 200

    after = sql.execute(
        "select coalesce(sum(amount), 0) from facts "
        "where period = '2026-06-01' and superseded_at is null"
    ).fetchone()[0]
    assert after == before, "закрытый месяц переписан разбором — это запрещено"

    marks = {row[0].split("#")[-1] if "#" in row[0] else "primary": row for row in facts(sql)}
    assert marks["primary"][3] == "unclassified"     # исходная строка цела
    assert marks["storno"][1] == Decimal("-24000.00")
    assert marks["fix"][3] == "food_cost"            # разобранная запись рядом
    assert "закрыт" in body(answer)
