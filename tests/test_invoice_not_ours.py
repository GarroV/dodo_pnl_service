"""Накладную можно пометить «не наша», и она уходит из очереди (issue #174, T205).

Модуль 15 эталона: рядом с разбором стоит кнопка «Не наша» — документ чужого
юрлица, ошибка почты поставщика, бумага соседнего арендатора. Сегодня такую
накладную приходится либо разбирать как свою (и она навсегда остаётся в P&L
неверной суммой), либо оставлять в очереди навсегда — очередь при этом перестаёт
быть рабочим списком.

**Пометка не стирает документ.** Он остаётся со следом: кто и когда решил, что
бумага чужая. Стёртый документ означал бы, что через месяц ту же бумагу принесут
второй раз и разберут как новую — а объяснить, почему её нет, будет нечем.

**Строки P&L при этом сторнируются.** Расход, который уже стоял в отчёте,
обязан из него уйти — но не исчезновением строки, а сторно (D020): июнь сегодня
и июнь через полгода должны давать одно число.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import body, login_as
from test_supplier_invoices import (  # noqa: F401
    NEW,
    counterparty,
    invoice_form,
    invoices_removed,
    item,
    key,
    sql,
    tenant,
    units,
)


@pytest.fixture
def invoice(client, sql, counterparty, units, invoices_removed):  # noqa: F811
    """Счёт на 24 000, уже стоящий в P&L."""
    login_as(client, "accountant")
    answer = client.post(NEW, invoice_form(counterparty, units, number="ALIEN-1"))
    assert answer.status_code == 302, body(answer)
    document = sql.execute(
        "select id from source_documents where doc_number = 'ALIEN-1'"
    ).fetchone()[0]
    return f"/invoices/{document}/"


def in_pnl(sql) -> Decimal:  # noqa: F811
    """Сколько денег этой бумаги стоит в P&L сейчас."""
    return sql.execute(
        """select coalesce(sum(amount), 0) from facts
            where dedup_key like 'manual:invoice:%%' and superseded_at is null"""
    ).fetchone()[0]


def state_of(sql):  # noqa: F811
    return sql.execute(
        "select not_ours_at is not null, not_ours_by is not null from source_documents "
        "where doc_number = 'ALIEN-1'"
    ).fetchone()


# --- ядро ---------------------------------------------------------------------


def test_a_stranger_invoice_leaves_the_pnl(client, sql, invoice):  # noqa: F811
    """Пометили «не наша» — денег этой бумаги в отчёте больше нет."""
    assert in_pnl(sql) == Decimal("24000.00"), "счёт не встал в P&L — проверяем не то"

    answer = client.post(invoice + "not-ours/", {"why": "Бумага соседнего арендатора"},
                         follow=True)
    assert answer.status_code == 200, body(answer)[:400]
    assert in_pnl(sql) == Decimal("0.00"), "сумма чужой бумаги осталась в отчёте"


def test_the_document_stays_with_a_trace(client, sql, invoice):  # noqa: F811
    """Документ не стирается: остаётся след, кто и когда так решил."""
    client.post(invoice + "not-ours/", {"why": "Бумага соседнего арендатора"}, follow=True)

    marked, by_whom = state_of(sql)
    assert marked, "документ не помечен чужим"
    assert by_whom, "не видно, кто так решил"


def test_the_reason_is_required(client, sql, invoice):  # noqa: F811
    """Без причины пометить нельзя: через полгода «не наша» без слов не объяснить."""
    answer = client.post(invoice + "not-ours/", {"why": ""})

    assert answer.status_code == 400, body(answer)[:300]
    assert in_pnl(sql) == Decimal("24000.00"), "сумма ушла из отчёта без причины"
    assert not state_of(sql)[0]


def test_a_marked_invoice_is_out_of_the_list(client, sql, invoice):  # noqa: F811
    """Чужая бумага уходит из списка счетов: список — рабочая очередь."""
    client.post(invoice + "not-ours/", {"why": "Чужое юрлицо"}, follow=True)

    shown = body(client.get("/invoices/"))
    assert "ALIEN-1" not in shown, "чужая бумага осталась в очереди"


def test_the_card_says_it_is_not_ours(client, sql, invoice):  # noqa: F811
    """На самой карточке видно, что бумага признана чужой, и почему."""
    client.post(invoice + "not-ours/", {"why": "Бумага соседнего арендатора"}, follow=True)

    shown = body(client.get(invoice))
    assert "не наша" in shown.lower()
    assert "Бумага соседнего арендатора" in shown


def test_the_manager_cannot_mark_a_stranger_invoice(client, sql, invoice):  # noqa: F811
    """Решает тот, кто ведёт месяц: у управляющего точки этого счёта нет вовсе."""
    login_as(client, "manager")
    answer = client.post(invoice + "not-ours/", {"why": "Чужое"})

    assert answer.status_code in (403, 404), body(answer)[:300]
    assert in_pnl(sql) == Decimal("24000.00")
