"""Счета и инбокс по HTTP: тот же ответ, что у экрана (T153).

Способ приёмки здесь тот же, что у расходов (`tests/test_expenses_api.py`), и
выбран он не для симметрии: **каждый сценарий гоняется дважды — экраном и
вызовом — и ответы сравниваются**. Иначе две поверхности разъезжаются молча,
потому что экран смотрят глазами, а вызов нет. Ровно так на третьей очереди
`/expenses/?ledger=` показывал две строки, а `/api/expenses/?ledger=` — одну.

Отдельно проверяется то, ради чего вызов вообще написан: он **не имеет
собственного доступа**. Тенанта и роли в параметрах нет, срез делает та же база;
записывающие вызовы отвечают только на POST; отказ по чужому счёту неотличим от
отказа по выдуманному.
"""
from __future__ import annotations

import json
from decimal import Decimal

from conftest import body, login_as
from test_directory import sql  # noqa: F401
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

API = "/api/invoices/"
WINDOW = {"from": "2026-07-01", "to": "2026-07-31"}


def answer(response) -> dict:
    return json.loads(response.content.decode())


# --- список -------------------------------------------------------------------


def test_the_call_and_the_screen_show_the_same_invoices(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Одна дверь, две ручки: суммы в ответе те же, что в разметке экрана."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item))
    document = lines(sql)[0][7]
    client.post(f"/invoices/{document}/pay/", {
        "date": "2026-07-20", "amount": "10000.00", "entry_key": key(),
    })

    given = answer(client.get(API, WINDOW))
    shown = body(client.get("/invoices/", WINDOW))

    assert given["count"] == 1
    assert given["total"] == "24000.00"
    assert given["outstanding"] == "14000.00"
    # Посимвольно те же строки, что уехали в разметку: сверить ответ с экраном
    # можно, не разбирая HTML.
    assert f'data-amount="{given["rows"][0]["amount"]}"' in shown
    assert f'data-left="{given["rows"][0]["left"]}"' in shown
    assert f'data-state="{given["rows"][0]["state"]}"' in shown


def test_the_ledger_slice_answers_the_same_on_both_surfaces(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """`?ledger=` разбирается ОДНИМ разбором: ответы обязаны совпасть."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item))
    client.post(NEW, invoice_form(counterparty, units, item=item, number="EPS-2",
                                  ledger="internal", amount="1000.00"))

    for cut, expected in (("official", 1), ("internal", 1), ("", 2)):
        given = answer(client.get(API, dict(WINDOW, ledger=cut)))
        shown = body(client.get("/invoices/", dict(WINDOW, ledger=cut)))
        assert given["count"] == expected
        assert shown.count("data-invoice=") == expected


def test_an_unknown_ledger_gives_emptiness_on_both(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Выдуманное слово отвечает тем же, чем невидимый регистр, — пустотой (D023)."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item))

    given = answer(client.get(API, dict(WINDOW, ledger="secret")))
    shown = body(client.get("/invoices/", dict(WINDOW, ledger="secret")))
    assert given["count"] == 0
    assert shown.count("data-invoice=") == 0


def test_the_window_is_bounded_and_says_there_is_more(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Вызову неограниченную выборку отдавать нельзя: окно и признак «есть ещё»."""
    login_as(client, "accountant")
    for number in range(3):
        client.post(NEW, invoice_form(counterparty, units, item=item,
                                      number=f"EPS-{number}"))

    given = answer(client.get(API, dict(WINDOW, limit="2")))
    assert given["count"] == 2
    assert given["has_more"] is True
    assert given["limit"] == 2

    given = answer(client.get(API, dict(WINDOW, limit="10")))
    assert given["has_more"] is False


def test_a_broken_window_is_refused_not_defaulted(
    client, invoices_removed,  # noqa: F811
):
    """Тихое умолчание означало бы, что бот листает не то, что думает."""
    login_as(client, "accountant")
    refused = client.get(API, dict(WINDOW, limit="много"))
    assert refused.status_code == 400
    assert "limit" in answer(refused)["error"]


# --- запись -------------------------------------------------------------------


def test_the_call_records_an_invoice_exactly_like_the_form(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Три даты разводятся и здесь: разбор у вызова и у формы один."""
    login_as(client, "accountant")
    given = answer(client.post(API, invoice_form(counterparty, units, item=item)))

    assert given["action"] == "inserted"
    assert given["period"] == "2026-06"       # период учёта
    assert given["moved_from"] is None
    assert given["invoice_id"]

    (row,) = lines(sql)
    assert row[2].isoformat() == "2026-06-01"
    assert row[3].isoformat() == "2026-07-03"


def test_the_call_accepts_json_too(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Тело JSON кладётся туда, откуда его читает ТОТ ЖЕ разбор формы."""
    login_as(client, "accountant")
    given = answer(client.post(
        API, json.dumps(invoice_form(counterparty, units, item=item)),
        content_type="application/json",
    ))
    assert given["action"] == "inserted"
    assert len(lines(sql)) == 1


def test_the_same_key_twice_changes_nothing(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Повторный вызов с тем же ключом не заводит второго счёта."""
    login_as(client, "accountant")
    form = invoice_form(counterparty, units, item=item)
    first = answer(client.post(API, form))
    second = answer(client.post(API, form))

    assert first["action"] == "inserted"
    assert second["action"] == "unchanged"
    assert len(lines(sql)) == 1


def test_paying_reports_what_is_left(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Частичная оплата отвечает остатком числом, а не «оплачено да/нет»."""
    login_as(client, "accountant")
    document = answer(client.post(API, invoice_form(counterparty, units,
                                                    item=item)))["invoice_id"]

    given = answer(client.post(f"/api/invoices/{document}/pay/", {
        "date": "2026-08-04", "amount": "10000.00", "entry_key": key(),
    }))
    assert given["period"] == "2026-08"        # месяц денег, а не месяц счёта
    assert given["paid"] == "10000.00"
    assert given["left"] == "14000.00"


def test_the_card_shows_the_invoice_with_its_payments(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    login_as(client, "accountant")
    document = answer(client.post(API, invoice_form(counterparty, units,
                                                    item=item)))["invoice_id"]
    client.post(f"/api/invoices/{document}/pay/", {
        "date": "2026-08-04", "amount": "24000.00", "entry_key": key(),
    })

    given = answer(client.get(f"/api/invoices/{document}/"))
    assert given["invoice"]["state"] == "paid"
    assert given["invoice"]["left"] == "0"
    assert len(given["payments"]) == 1
    assert given["payments"][0]["amount"] == "24000.00"
    assert given["payments"][0]["channel"] == "bank"


# --- инбокс -------------------------------------------------------------------


def test_the_inbox_answers_the_same_number_as_the_screen(
    client, sql, counterparty, units, invoices_removed,  # noqa: F811
):
    """Сумма без статьи — одна и та же цифра на экране и в ответе."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=""))

    given = answer(client.get("/api/inbox/"))
    shown = body(client.get("/inbox/"))
    assert given["count"] == 1
    assert given["total"] == "24000.00"
    assert f'data-total="{given["total"]}"' in shown


def test_the_call_classifies_a_line(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=""))
    fact_id = answer(client.get("/api/inbox/"))["rows"][0]["id"]

    given = answer(client.post(f"/api/inbox/{fact_id}/classify/", {
        "item": item, "unit": units["NS1"],
    }))
    assert given["action"] == "updated"
    assert given["period"] == "2026-06"        # период учёта не двинулся
    assert answer(client.get("/api/inbox/"))["count"] == 0


def test_classifying_without_an_article_is_refused_in_words(
    client, sql, counterparty, units, invoices_removed,  # noqa: F811
):
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=""))
    fact_id = answer(client.get("/api/inbox/"))["rows"][0]["id"]

    refused = client.post(f"/api/inbox/{fact_id}/classify/", {"unit": units["NS1"]})
    assert refused.status_code == 400
    assert "Статья расхода" in answer(refused)["error"]


# --- доступ и метод -----------------------------------------------------------


def test_writing_calls_answer_only_to_post(client, invoices_removed):  # noqa: F811
    """GET на запись — 405 с `Allow`, а не тихий показ."""
    login_as(client, "accountant")
    for url in ("/api/payments/",
                "/api/invoices/00000000-0000-4000-8000-0000000000b1/pay/",
                "/api/inbox/00000000-0000-4000-8000-0000000000b2/classify/"):
        refused = client.get(url)
        assert refused.status_code == 405, url
        assert refused.headers["Allow"] == "POST"


def test_without_a_session_nothing_is_shown(client, invoices_removed):  # noqa: F811
    client.post("/logout/")
    refused = client.get(API)
    assert refused.status_code == 401
    assert "войдите" in answer(refused)["error"].lower()


def test_a_stranger_invoice_is_indistinguishable_from_a_made_up_one(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Управляющий чужой точки получает то же 404, что и на выдуманный номер."""
    login_as(client, "accountant")
    document = answer(client.post(API, invoice_form(counterparty, units,
                                                    item=item)))["invoice_id"]

    login_as(client, "manager")          # управляющий NS1, счёт на BG1
    alien = client.get(f"/api/invoices/{document}/")
    nobody = client.get("/api/invoices/00000000-0000-4000-8000-0000000000b3/")
    assert alien.status_code == nobody.status_code == 404
    assert answer(alien) == answer(nobody)


def test_the_manager_does_not_see_the_invoices_of_another_unit(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Срез делает база: в ответе вызова его ровно столько же, сколько на экране."""
    login_as(client, "accountant")
    client.post(API, invoice_form(counterparty, units, item=item))            # BG1
    client.post(API, invoice_form(counterparty, units, item=item, number="NS",
                                  unit=units["NS1"]))

    login_as(client, "manager")
    given = answer(client.get(API, WINDOW))
    shown = body(client.get("/invoices/", WINDOW))
    assert given["count"] == 1
    assert shown.count("data-invoice=") == 1
    assert given["rows"][0]["unit"] == "NS1"


def test_the_totals_are_the_sum_of_the_shown_rows(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Итог — сложение показанных строк, а не соседняя выборка."""
    login_as(client, "accountant")
    client.post(API, invoice_form(counterparty, units, item=item))
    client.post(API, invoice_form(counterparty, units, item=item, number="EPS-2",
                                  amount="1000.00"))

    given = answer(client.get(API, WINDOW))
    assert Decimal(given["total"]) == sum(
        Decimal(row["amount"]) for row in given["rows"]
    )
    assert Decimal(given["outstanding"]) == sum(
        Decimal(row["left"]) for row in given["rows"]
    )
