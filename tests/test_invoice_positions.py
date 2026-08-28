"""Накладную можно разложить на позиции с разными статьями (issue #174, T204).

Модуль 3 эталона, вкладка «Разбор документа»: одна бумага раскрывается
позициями, у каждой своя статья и своя точка, а внизу видно, сходится ли сумма
позиций с суммой документа — «не сходится с позициями на 184 320». Пока не
сойдётся, проводить нельзя.

Зачем это нужно. Накладная из Метро — это еда и канцелярия в одной бумаге, и это
**разные строки P&L**. Пока документ можно отнести только к одной статье,
бухгалтер либо ставит одну на всё (и P&L врёт), либо заводит два счёта на одну
бумагу (и оплата разъезжается с документом).

Форма данных это уже позволяла: позиция и есть факт (`document_id` + `line_no`),
отдельной таблицы позиций в схеме нет намеренно (`0230_facts`). Не хватало двух
вещей — экрана и **сверки с суммой документа**: без неё разложить можно, а
заметить потерянную позицию нечем.
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
    """Счёт на 24 000: одна позиция, как его вносят сегодня.

    Адрес карточки берётся из базы, а не из редиректа: после записи продукт
    уводит на СПИСОК счетов, и «адрес ответа плюс хвост» дал бы мусор, который
    Django разобрал бы как список — тест при этом выглядел бы работающим.
    """
    login_as(client, "accountant")
    answer = client.post(NEW, invoice_form(counterparty, units, number="POS-1"))
    assert answer.status_code == 302, body(answer)
    document = sql.execute(
        "select id from source_documents where doc_number = 'POS-1'"
    ).fetchone()[0]
    return f"/invoices/{document}/"


def positions(sql):  # noqa: F811
    return sql.execute(
        """select f.line_no, f.amount, e.code
             from facts f left join expense_items e on e.id = f.expense_item_id
            where f.dedup_key like 'manual:invoice:%%' and f.superseded_at is null
            order by f.line_no nulls first, f.created_at"""
    ).fetchall()


def add_position(client, url, **fields):
    """Добавить позицию так, как это делает форма разбора документа.

    Точка обязательна и у позиции — как у счёта: расход без точки не попадёт ни
    в один отчёт по точкам. «Вся сеть» тоже ответ, и она передаётся так же.
    """
    form = {"amount": "4000.00", "note": "Канцелярия", "unit": "network", **fields}
    return client.post(url + "positions/", form, follow=True)


# --- ядро ---------------------------------------------------------------------


def test_a_second_position_gets_its_own_article(client, sql, item, invoice):  # noqa: F811
    """Вторая позиция той же бумаги живёт со своей статьёй и своей суммой."""
    answer = add_position(client, invoice, item=item, amount="4000.00")
    assert answer.status_code == 200, body(answer)[:400]

    rows = positions(sql)
    assert len(rows) == 2, f"позиций не две: {rows}"
    assert sum(row[1] for row in rows) == Decimal("28000.00")


def test_the_card_says_the_positions_do_not_add_up(client, sql, item, invoice):  # noqa: F811
    """Сумма позиций разошлась с суммой документа — это сказано числом.

    Эталон: «не сходится с позициями на N». Без этой строки разложить можно, а
    заметить потерянную позицию нечем — расхождение всплывёт на сборке P&L.
    """
    add_position(client, invoice, item=item, amount="4000.00")
    shown = body(client.get(invoice))

    assert "не сходится" in shown.lower(), "расхождение не названо"
    assert "4 000" in shown or "4000" in shown, "не сказано, на сколько разошлось"


def test_the_card_says_it_adds_up_when_it_does(client, sql, item, invoice):  # noqa: F811
    """Сошлось — так и сказано: молчание читается как «не проверяли»."""
    shown = body(client.get(invoice))
    assert "сходится" in shown.lower()
    assert "не сходится" not in shown.lower()


def test_a_position_without_an_amount_is_refused(client, sql, item, invoice):  # noqa: F811
    """Позиция без суммы — отказ словами, а не строка на ноль."""
    answer = add_position(client, invoice, item=item, amount="")
    assert answer.status_code == 400, body(answer)[:300]
    assert len(positions(sql)) == 1


def test_the_sum_of_positions_is_the_sum_of_the_invoice(client, sql, item, invoice):  # noqa: F811
    """Сумма счёта — это сложение позиций, а не отдельная колонка.

    Колонка была бы вторым ответом на тот же вопрос и разошлась бы с первым на
    первой же добавленной позиции — молча. Тот же довод, что у оплаты
    (`_summary`).
    """
    add_position(client, invoice, item=item, amount="4000.00")
    shown = body(client.get(invoice))
    assert "28 000" in shown, "сумма счёта не выросла на позицию"


def test_a_position_keeps_the_ledger_of_the_document(client, sql, item, invoice):  # noqa: F811
    """Позиция ложится в тот же регистр, что и счёт: бумага одна.

    Регистр у позиции не спрашивается вовсе. Половина бумаги в официальном, а
    половина в дополнительном — это не позиции одного документа, а два разных
    документа, и заводятся они отдельно.
    """
    add_position(client, invoice, item=item, amount="4000.00")
    ledgers = sql.execute(
        """select distinct ledger::text from facts
            where dedup_key like 'manual:invoice:%%' and superseded_at is null"""
    ).fetchall()
    assert ledgers == [("official",)], f"позиция ушла в другой регистр: {ledgers}"


def test_a_repeated_submit_does_not_double_the_position(client, sql, item, invoice):  # noqa: F811
    """Дважды отправленная форма не добавляет позицию дважды.

    Ключ позиции приезжает из формы (`entry_key`), поэтому повторная отправка
    той же формы заменяет ту же строку, а не плодит новые. Без этого двойной
    щелчок удваивал бы расход.
    """
    same = "position-key-1"
    add_position(client, invoice, item=item, amount="4000.00", entry_key=same)
    add_position(client, invoice, item=item, amount="4000.00", entry_key=same)

    rows = positions(sql)
    assert len(rows) == 2, f"повторная отправка удвоила позицию: {rows}"


def test_the_manager_cannot_add_a_position_to_a_stranger_invoice(client, sql, item, invoice):  # noqa: F811
    """Чужой счёт для управляющего точки не существует — и позиции ему не добавить."""
    login_as(client, "manager")
    answer = client.post(invoice + "positions/",
                         {"amount": "4000.00", "item": item, "unit": "network"})
    assert answer.status_code in (403, 404), body(answer)[:300]
    assert len(positions(sql)) == 1

