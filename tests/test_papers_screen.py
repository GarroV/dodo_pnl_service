"""Управляющий приносит бумагу, бухгалтер её разбирает (T174, D047).

Главное, что здесь проверяется, — **одно утверждение о деньгах**: принесённая
бумага в P&L не входит, пока её не разобрали. Не «помечена как неподтверждённая»,
а именно не входит: в отчёте живут строки учёта, а у бумаги их ноль. Это ровно то
свойство, которое отличает сбор первички от прямой записи в P&L человеком,
который не знает ни статьи, ни периода.

Рядом — правила, которые обязаны работать здесь так же, как на соседних экранах:
повторная отправка формы не заводит вторую бумагу, чужая точка отвергается базой
(D014) и отвечает неотличимо от несуществующей (D023), а отказ не оставляет
бумагу «наполовину принятой».

Проверки идут **через экраны**, то есть тем же путём, что человек. Прямое чтение
базы — только осмотр результата, и идёт оно владельцем схемы: это не проверка
доступа. Доступ проверяется отдельно и ролью `app_user`
(`tests/test_paper_access.py`).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from conftest import body, login_as
from test_directory import payruns_restored, sql  # noqa: F401
from test_supplier_invoices import counterparty, item, tenant, units  # noqa: F401

PAPERS = "/papers/"
NEW = "/papers/new/"
INBOX = "/inbox/"
JULY_DAY = "2026-07-03"

# Настоящая подпись JPEG: продукт определяет тип по байтам, а не по имени файла
# и не по слову браузера, и проверка обязана идти тем же путём.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"snapshot-of-a-delivery-note"


def shot(name: str = "note.jpg", data: bytes = JPEG, content_type: str = "image/jpeg"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def key() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def papers_removed(sql, tenant):  # noqa: F811
    """Бумаги теста не переживают его.

    Просить эту фикстуру нужно **раньше** `payruns_restored`: разбираются они в
    обратном порядке, а строку закрытого месяца база удалить не даст
    (`facts_guard`), пока месяц не открыт заново.
    """
    before = [
        row[0] for row in sql.execute(
            "select period from periods where tenant_id = %s", (tenant,)
        ).fetchall()
    ]
    yield
    sql.execute(
        "delete from facts where document_id in "
        "(select id from source_documents where external_id like 'paper:%%')"
    )
    sql.execute(
        "delete from document_files where document_id in "
        "(select id from source_documents where external_id like 'paper:%%')"
    )
    sql.execute("delete from source_documents where external_id like 'paper:%%'")
    sql.execute(
        """delete from periods p
            where p.tenant_id = %s
              and p.period <> all(%s::date[])
              and not exists (select 1 from facts f
                               where f.tenant_id = p.tenant_id and f.period = p.period)
              and not exists (select 1 from payruns r
                               where r.tenant_id = p.tenant_id and r.period = p.period)""",
        (tenant, before),
    )


def hand_over(client, units, *, kind="invoice", unit="NS1",  # noqa: F811
              amount="18600.00", note="Delivery note from the warehouse",
              entry_key=None, file=None, **extra):
    """Скинуть бумагу так, как это делает человек: форма и файл одним POST."""
    form = {
        "entry_key": entry_key or key(),
        "kind": kind,
        "date": JULY_DAY,
        "note": note,
        "scan": shot() if file is None else file,
        **extra,
    }
    if unit is not None:
        form["unit"] = units[unit]
    if amount is not None:
        form["amount"] = amount
    return client.post(NEW, form)


def card_of(response) -> str:
    """Адрес карточки, на которую увёл продукт после приёма бумаги."""
    assert response.status_code == 302, body(response)
    return response["Location"]


def sum_in_pnl(sql, document_id) -> Decimal:  # noqa: F811
    """Сколько денег этого документа лежит в P&L. Ноль — их там нет вовсе."""
    return sql.execute(
        "select coalesce(sum(amount), 0) from pnl_lines where document_id = %s",
        (str(document_id),),
    ).fetchone()[0]


def document_id_of(sql, external_like="paper:%"):  # noqa: F811
    rows = sql.execute(
        "select id from source_documents where external_id like %s", (external_like,)
    ).fetchall()
    assert len(rows) == 1, f"документов не один, а {len(rows)}"
    return rows[0][0]


# --- бумага приходит с точки --------------------------------------------------


def test_the_manager_hands_over_a_delivery_note(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Управляющий скидывает накладную своей точки — и она принята."""
    login_as(client, "manager")
    landed = card_of(hand_over(client, units))

    card = body(client.get(landed))
    assert 'data-waiting="1"' in card, card
    assert "Накладная" in card
    listed = body(client.get(PAPERS))
    assert 'data-waiting="1"' in listed
    assert "NS1" in listed


def test_the_photograph_comes_back_byte_for_byte(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Файл отдаётся тем же, каким пришёл: разбирают именно его, а не миниатюру."""
    login_as(client, "manager")
    card_of(hand_over(client, units))
    document_id = document_id_of(sql)

    answer = client.get(f"/papers/{document_id}/file/")
    assert answer.status_code == 200
    assert answer["Content-Type"] == "image/jpeg"
    assert answer["X-Content-Type-Options"] == "nosniff"
    assert answer.content == JPEG


def test_a_paper_without_a_stated_amount_shows_a_dash_not_a_zero(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Сумму управляющий может не знать. Прочерк и ноль — разные вещи.

    Ноль в этой колонке читался бы как «бумага на нулевую сумму», а прочерк — как
    «сумма неизвестна», и это первое правило дизайн-системы о числах.
    """
    login_as(client, "manager")
    card_of(hand_over(client, units, amount=None))

    listed = body(client.get(PAPERS))
    assert "num num--empty" in listed, listed


def test_the_same_form_twice_hands_over_one_paper(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Второе нажатие на телефоне с плохой связью — та же бумага, не вторая."""
    login_as(client, "manager")
    once = key()
    hand_over(client, units, entry_key=once)
    hand_over(client, units, entry_key=once)

    assert sql.execute(
        "select count(*) from source_documents where external_id like 'paper:%%'"
    ).fetchone()[0] == 1
    assert sql.execute("select count(*) from document_files").fetchone()[0] == 1


# --- бумаги нет в P&L, пока её не разобрали ------------------------------------


def test_a_handed_paper_is_not_in_the_pnl_at_all(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Главное утверждение задачи: до разбора денег бумаги в отчёте нет.

    Сломайте это — и управляющий начнёт вносить расходы в P&L, называя суммы со
    слов: без статьи, без периода учёта и без разбора бухгалтером.
    """
    login_as(client, "manager")
    card_of(hand_over(client, units, amount="18600.00"))
    document_id = document_id_of(sql)

    assert sum_in_pnl(sql, document_id) == 0
    # И сумма при этом не потеряна: она видна человеку на экране.
    assert "18 600,00" in body(client.get(PAPERS))


def test_a_handed_paper_stands_in_the_classification_inbox(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Бумага стоит в инбоксе классификации и видна числом.

    Отдельным списком от строк без статьи: те **уже** в P&L, только не в той
    статье, а бумаги в P&L нет вовсе. Общий итог сложил бы два числа, которых
    вместе не существует ни в одном отчёте.
    """
    login_as(client, "manager")
    card_of(hand_over(client, units, amount="18600.00"))

    login_as(client, "accountant")
    inbox = body(client.get(INBOX))
    assert 'data-papers="1"' in inbox, inbox
    assert 'data-stated="18600.00"' in inbox


# --- разбор -------------------------------------------------------------------


def review(client, *, counterparty, item, units,  # noqa: F811
           unit="NS1", amount="18600.00", document_id, **extra):
    """Разобрать бумагу с её же карточки: поля те же, что у счёта."""
    return client.post(f"/papers/{document_id}/", {
        "entry_key": key(),
        "date": JULY_DAY,
        "period": "2026-07",
        "counterparty": counterparty,
        "item": item,
        "unit": units[unit],
        "ledger": "official",
        "amount": amount,
        **extra,
    })


def test_the_accountant_sorts_the_paper_out_and_the_money_appears(
    client, units, counterparty, item, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Разбор бухгалтером: строка появляется в P&L, документ остаётся ТОТ ЖЕ.

    Второй документ был бы худшим из исходов: бумага осталась бы стоять в
    инбоксе с фотографией, а деньги уехали бы в документ-двойник, который никто
    не открывал.
    """
    login_as(client, "manager")
    card_of(hand_over(client, units))
    document_id = document_id_of(sql)

    login_as(client, "accountant")
    answer = review(client, counterparty=counterparty, item=item, units=units,
                    document_id=document_id)
    assert answer.status_code == 302, body(answer)

    assert document_id_of(sql) == document_id
    assert sum_in_pnl(sql, document_id) == Decimal("18600.00")

    card = body(client.get(f"/papers/{document_id}/"))
    assert 'data-waiting="0"' in card, card
    assert f"/invoices/{document_id}/" in card


def test_a_sorted_out_paper_leaves_the_inbox(
    client, units, counterparty, item, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Разобранная бумага уходит из очереди: иначе очередь перестают читать."""
    login_as(client, "manager")
    card_of(hand_over(client, units))
    document_id = document_id_of(sql)

    login_as(client, "accountant")
    review(client, counterparty=counterparty, item=item, units=units,
           document_id=document_id)

    inbox = body(client.get(INBOX))
    assert 'data-papers="0"' in inbox, inbox


def test_a_receipt_stays_a_receipt_after_the_review(
    client, units, counterparty, item, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Чек остаётся чеком: разбор назначает статью, а не переписывает бумагу.

    Записать его счётом значило бы стереть то, что человек про бумагу знал, — и
    получить «счёт», которого поставщик никогда не выставлял.
    """
    login_as(client, "manager")
    card_of(hand_over(client, units, kind="receipt"))
    document_id = document_id_of(sql)

    login_as(client, "accountant")
    review(client, counterparty=counterparty, item=item, units=units,
           document_id=document_id)

    assert sql.execute(
        "select kind::text from source_documents where id = %s", (document_id,)
    ).fetchone()[0] == "receipt"
    # И карточка разобранного чека открывается, а не отвечает 404: иначе в
    # списке счетов осталась бы строка, ведущая в никуда.
    assert client.get(f"/invoices/{document_id}/").status_code == 200


def test_a_refused_review_leaves_the_paper_in_the_inbox(
    client, units, item, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Отказ на разборе не оставляет бумагу «наполовину разобранной».

    Документ без строк выглядел бы разобранным — молчаливый сбой, который в
    проекте-предшественнике стоил дорого. Здесь бумага обязана остаться в
    очереди и сказать об этом словами.
    """
    login_as(client, "manager")
    card_of(hand_over(client, units))
    document_id = document_id_of(sql)

    login_as(client, "accountant")
    answer = client.post(f"/papers/{document_id}/", {
        "entry_key": key(), "date": JULY_DAY, "item": item,
        "unit": units["NS1"], "ledger": "official", "amount": "18600.00",
        # Контрагента нет: без него счёт не на кого выписать.
    })
    assert answer.status_code in (400, 409), answer.status_code
    assert sum_in_pnl(sql, document_id) == 0
    assert 'data-waiting="1"' in body(client.get(f"/papers/{document_id}/"))
    assert 'data-papers="1"' in body(client.get(INBOX))


# --- чужое и негодное ---------------------------------------------------------


def test_the_manager_cannot_hand_over_a_paper_for_another_unit(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Подмена точки в форме отвергается базой, и бумага не заводится."""
    login_as(client, "manager")
    answer = hand_over(client, units, unit="BG1")

    assert answer.status_code in (400, 409, 403), answer.status_code
    assert sql.execute(
        "select count(*) from source_documents where external_id like 'paper:%%'"
    ).fetchone()[0] == 0


def test_a_stranger_paper_is_indistinguishable_from_a_made_up_one(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Бумага чужой точки и выдуманный номер отвечают одинаково — 404 (D023)."""
    login_as(client, "accountant")
    card_of(hand_over(client, units, unit="BG1"))
    document_id = document_id_of(sql)

    login_as(client, "manager")
    assert client.get(f"/papers/{document_id}/").status_code == 404
    assert client.get(f"/papers/{document_id}/file/").status_code == 404
    assert client.get(f"/papers/{uuid.uuid4()}/").status_code == 404


def test_a_file_that_is_not_a_photograph_is_refused_in_words(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Тип определяется по байтам: переименованный в .jpg текст не проходит."""
    login_as(client, "manager")
    answer = hand_over(
        client, units,
        file=shot("note.jpg", b"just some text, not a photograph", "image/jpeg"),
    )

    assert answer.status_code in (400, 409), answer.status_code
    assert "PDF" in body(answer)
    assert sql.execute("select count(*) from document_files").fetchone()[0] == 0


def test_a_paper_without_a_file_is_refused(
    client, units, papers_removed, payruns_restored, sql,  # noqa: F811
):
    """Без снимка разбирать нечего, и такая бумага в очередь не встаёт."""
    login_as(client, "manager")
    answer = client.post(NEW, {
        "entry_key": key(), "kind": "invoice", "date": JULY_DAY,
        "unit": units["NS1"], "amount": "18600.00",
    })

    assert answer.status_code in (400, 409), answer.status_code
    assert sql.execute(
        "select count(*) from source_documents where external_id like 'paper:%%'"
    ).fetchone()[0] == 0


def test_nobody_gets_a_paper_without_a_session(client, papers_removed):  # noqa: F811
    """Без входа не отдаётся ни список, ни файл: это данные партнёра."""
    assert client.get(PAPERS).status_code in (302, 403)
    assert client.get(NEW).status_code in (302, 403)

