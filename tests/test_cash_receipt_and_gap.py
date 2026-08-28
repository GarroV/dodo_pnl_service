"""Чек к расходу и разрыв «ушло из кассы / принято в P&L» (T184, модуль 6 эталона).

Модуль 6 стоит на одной мысли: **наличный расход — два независимых факта**.
Деньги ушли из кассы в тот момент, когда управляющий их отдал. В P&L расход
входит по своим правилам, и это не одно и то же число. Разрыв между ними и есть
контроль кассы, поэтому он показан всегда — включая случай, когда он нулевой.

Проверяется поэтому не «на экране появились три числа», а то, из-за чего они
расходятся:

* **перевод** (`kind = 'transfer'`) — деньги из кассы вышли, а в P&L его нет
  вовсе: представления `pnl_by_unit` и `pnl_by_network` переводы исключают;
* **расход, учтённый в другом месяце** — трата июньская, а период учёта
  августовский (так ложится правка закрытого месяца, D020).

Числа сверяются с базой, а не друг с другом: три числа, посчитанные из одного
списка тремя способами, сойдутся и при неверном правиле.

Вторая половина файла — про сам чек. Главное здесь не «файл сохранился», а
**кому он виден**: политика `follows_its_expense` зовёт сам факт, и проверка
идёт ролью `app_user`, потому что владелец схемы политики обходит и зелёный
результат под ним не значил бы ничего.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import (
    T1,
    U_BG1,
    U_NS1,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
    body,
    login_as,
)

# Однопиксельный PNG: настоящие байты с настоящей подписью формата. Выдуманные
# байты прошли бы запись, но не прошли бы разбор типа по подписи — то есть
# проверяли бы не тот путь, которым ходит телефон управляющего.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# --- разрыв: экранная половина -------------------------------------------------


@pytest.fixture
def sql(web_env):
    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def gap_data(sql):
    """Три расхода одного дня: обычный, перевод и учтённый в другом месяце.

    Суммы разные и несократимые, чтобы ни одно из трёх чисел нельзя было
    получить случайно из другого: 1000 + 300 + 70.
    """
    line = sql.execute(
        "select id from pnl_items where code = 'food_cost'"
    ).fetchone()[0]
    moved = sql.execute(
        "select id from pnl_items where kind = 'transfer' limit 1"
    ).fetchone()
    assert moved is not None, "в справочнике нет строки-перевода — проверять нечего"
    tenant = sql.execute("select id from tenants order by code limit 1").fetchone()[0]
    unit = sql.execute(
        "select id from units where tenant_id = %s order by code limit 1", [tenant]
    ).fetchone()[0]

    items = {}
    for code, pnl in (("t184-plain", line), ("t184-transfer", moved[0])):
        items[code] = sql.execute(
            """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
               values (%s, %s, %s, %s, '2020-01-01') returning id""",
            [tenant, code, Jsonb({"ru": code, "en": code, "sr-latn": code}), pnl],
        ).fetchone()[0]

    rows = (
        # ключ, статья, сумма, дата траты, период учёта
        ("t184-a", "t184-plain", "1000.00", "2026-02-10", "2026-02-01"),
        ("t184-b", "t184-transfer", "300.00", "2026-02-11", "2026-02-01"),
        ("t184-c", "t184-plain", "70.00", "2026-02-12", "2026-08-01"),
    )
    for key, code, amount, on, period in rows:
        pnl = line if code == "t184-plain" else moved[0]
        sql.execute("select upsert_fact(%s)", [Jsonb({
            "tenant_id": str(tenant), "period": period, "doc_date": on, "unit_id": str(unit),
            "pnl_item_id": str(pnl), "expense_item_id": str(items[code]),
            "amount": amount, "currency": "RSD", "title": code,
            "channel": "cash", "source": "manual",
            "dedup_key": "manual:cash:" + key, "allocation": "direct",
        })])
    try:
        yield {"unit": unit}
    finally:
        sql.execute("delete from cash_receipts where entry_key like 't184-%'")
        sql.execute("delete from facts where dedup_key like 'manual:cash:t184-%'")
        sql.execute("delete from expense_items where code like 't184-%'")


def _marks(page: str) -> dict[str, Decimal]:
    """Три числа с экрана — машиночитаемыми значениями, а не из отформатированных.

    Числа в разметке локализуются («1 000,00»), и сверка с ними означала бы
    сверку с языком страницы, а не с деньгами. Тот же приём, что у итога списка.

    Сравнивается `Decimal`, а не строка: «0» и «0.00» — одни и те же деньги, и
    проверка, которая их различает, краснела бы на форме записи, а не на смысле.
    """
    import re

    found = {}
    for name in ("cash", "pnl", "gap", "noreceipt"):
        hit = re.search(rf'data-{name}="([^"]*)"', page)
        found[name] = Decimal(hit.group(1)) if hit else None
    return found


REGISTER = "/expenses/?from=2026-02-01&to=2026-02-28"


def test_the_screen_shows_all_three_numbers(client, gap_data):
    """Ушло из кассы, принято в P&L и разрыв — три числа, а не одно."""
    login_as(client, "admin")
    seen = _marks(body(client.get(REGISTER)))
    assert seen["cash"] == Decimal("1370.00"), "ушло из кассы — сумма всех расходов месяца"
    assert seen["pnl"] == Decimal("1000.00"), "в P&L входит только обычный расход своего месяца"
    assert seen["gap"] == Decimal("370.00"), "разрыв — перевод плюс расход другого месяца"


def test_the_gap_matches_what_the_report_actually_counts(client, sql, gap_data):
    """«Принято в P&L» сверяется с отчётом, а не с соседним числом того же экрана.

    Три числа, посчитанные из одного списка тремя способами, сойдутся и при
    неверном правиле. Поэтому второе число сверяется с `pnl_by_unit` — тем самым
    представлением, из которого собирается отчёт партнёра.
    """
    login_as(client, "admin")
    seen = _marks(body(client.get(REGISTER)))
    counted = sql.execute(
        """select coalesce(sum(l.amount), 0) from pnl_lines l
            join facts f on f.id = l.fact_id
           where f.dedup_key like 'manual:cash:t184-%'
             and l.period = '2026-02-01' and l.kind <> 'transfer'"""
    ).fetchone()[0]
    assert seen["pnl"] == counted


def test_the_gap_says_why_it_is_not_zero(client, gap_data):
    """Разрыв без причины — число, с которым нечего делать.

    Названы обе причины и обе поимённо: перевод и расход, учтённый в другом
    месяце. Сказать «370,00 не принято» и замолчать причину значило бы отправить
    человека искать её глазами по всей таблице.
    """
    login_as(client, "admin")
    page = body(client.get(REGISTER))
    assert "перевод" in page.lower()
    assert "учтён в другом месяце" in page.lower() or "учтено в другом месяце" in page.lower()


def test_a_zero_gap_is_still_shown(client, sql, gap_data):
    """Нулевой разрыв показывается, а не прячется.

    Спрятанный ноль читается как «этого числа тут не бывает», и человек
    перестаёт его искать — а именно на нулевом разрыве и держится доверие к
    кассе. Проверяется отбором по одному дню, где остаётся только обычный расход.
    """
    login_as(client, "admin")
    seen = _marks(body(client.get("/expenses/?from=2026-02-10&to=2026-02-10")))
    assert seen["cash"] == Decimal("1000.00")
    assert seen["gap"] == Decimal("0")


# --- чек: продукт --------------------------------------------------------------


@pytest.fixture
def one_expense(client, sql):
    """Один расход, внесённый через продукт, и уборка за собой.

    Статья заводится тут же: справочник продукта поставляется пустым намеренно
    (Q015), и брать «любую действующую» тут просто не из чего.
    """
    login_as(client, "admin")
    tenant = sql.execute("select id from tenants order by code limit 1").fetchone()[0]
    unit = sql.execute(
        "select id from units where tenant_id = %s order by code limit 1", [tenant]
    ).fetchone()[0]
    line = sql.execute(
        "select id from pnl_items where code = 'food_cost'"
    ).fetchone()[0]
    item = sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 't184-receipt', %s, %s, '2020-01-01') returning id""",
        [tenant, Jsonb({"ru": "Чек", "en": "Receipt", "sr-latn": "Racun"}), line],
    ).fetchone()[0]

    answer = client.post("/expenses/new/", {
        "date": "2026-02-20", "amount": "555", "item": str(item),
        "unit": str(unit), "note": "T184 чек", "entry_key": "",
    })
    assert answer.status_code == 302, body(answer)
    fact = sql.execute(
        """select id, dedup_key from facts
            where note = 'T184 чек' and superseded_at is null limit 1"""
    ).fetchone()
    assert fact is not None
    key = fact[1].removeprefix("manual:cash:")
    try:
        yield {"id": fact[0], "key": key, "item": item, "unit": unit}
    finally:
        sql.execute("delete from cash_receipts where entry_key = %s", [key])
        sql.execute("delete from facts where note = 'T184 чек'")
        sql.execute("delete from expense_items where code = 't184-receipt'")


def _attach(client, fact, data: bytes = PNG, name: str = "cek.png"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return client.post(f"/expenses/{fact['id']}/receipt/", {
        "receipt": SimpleUploadedFile(name, data, content_type="image/png"),
    })


def test_a_receipt_is_attached_and_given_back(client, one_expense):
    """Чек прикладывается к расходу и отдаётся теми же байтами."""
    login_as(client, "admin")
    assert _attach(client, one_expense).status_code == 302

    got = client.get(f"/expenses/{one_expense['id']}/receipt/")
    assert got.status_code == 200
    assert got["Content-Type"] == "image/png"
    assert b"".join(got.streaming_content if got.streaming else [got.content]) == PNG


def test_the_card_shows_that_a_receipt_is_attached(client, one_expense):
    """На карточке видно, что чек есть, — иначе прикладывать его незачем."""
    login_as(client, "admin")
    before = body(client.get(f"/expenses/{one_expense['id']}/"))
    assert "Чека нет" in before

    _attach(client, one_expense)
    after = body(client.get(f"/expenses/{one_expense['id']}/"))
    assert "Чек приложен" in after


def test_an_expense_without_a_receipt_is_marked_in_the_register(client, one_expense):
    """Сумма без чека помечена: и строкой, и отдельным числом.

    Отдельное число нужно потому, что метка в строке отвечает на вопрос «у этой
    ли строки чек», а бухгалтеру нужен ответ на другой — «на сколько денег у нас
    бумаг нет». Пролистать сорок строк, складывая их глазами, он не станет.
    """
    login_as(client, "admin")
    seen = _marks(body(client.get(REGISTER)))
    assert seen["noreceipt"] == Decimal("555.00")

    _attach(client, one_expense)
    after = _marks(body(client.get(REGISTER)))
    assert after["noreceipt"] == Decimal("0")


def test_a_file_that_is_not_a_photo_is_refused_in_words(client, one_expense):
    """Не фотография и не PDF — отказ словами, а не запись мусора в базу."""
    login_as(client, "admin")
    answer = _attach(client, one_expense, data="это просто текст".encode(), name="cek.png")
    assert answer.status_code == 422, answer.status_code
    assert "PDF" in body(answer)


def test_the_receipt_survives_editing_the_expense(client, sql, one_expense):
    """Правка расхода заводит новую версию факта, а чек остаётся тот же.

    Ради этого чек и привязан к ключу записи, а не к строке факта: привязка к
    `id` осиротела бы на первой правке суммы — молча, потому что запись прошла
    бы успешно.
    """
    login_as(client, "admin")
    _attach(client, one_expense)
    edited = client.post(f"/expenses/{one_expense['id']}/", {
        "date": "2026-02-20", "amount": "556", "item": str(one_expense["item"]),
        "unit": str(one_expense["unit"]), "note": "T184 чек",
    })
    assert edited.status_code == 302, body(edited)

    fresh = sql.execute(
        """select id from facts
            where note = 'T184 чек' and superseded_at is null limit 1"""
    ).fetchone()[0]
    assert str(fresh) != str(one_expense["id"]), "правка не завела новую версию"
    assert "Чек приложен" in body(client.get(f"/expenses/{fresh}/"))


# --- чек: кому он виден --------------------------------------------------------


def test_the_receipt_is_visible_exactly_when_its_expense_is(db):
    """Чек виден ровно тому, кому виден расход, — и это держит база.

    Ролью `app_user`, а не владельцем схемы: владелец обходит `force row level
    security`, и запрет, проверенный под ним, зелен всегда. Расход заводится на
    точке BG1, а управляющий ведёт NS1 — то есть чек он не должен найти вовсе,
    даже зная его номер.
    """
    from facts_helpers import fact_payload, upsert_fact

    upsert_fact(db, fact_payload(unit=U_BG1, key="manual:cash:t184-policy"))
    db.execute(
        """insert into cash_receipts
               (tenant_id, entry_key, media_type, byte_size, content, sha256)
           values (%s, 't184-policy', 'image/png', %s, %s, 'x')""",
        [T1, len(PNG), PNG],
    )

    with as_app_user(db, USER_DIRECTOR):
        seen = db.execute(
            "select count(*) from cash_receipts where entry_key = 't184-policy'"
        ).fetchone()[0]
    with as_app_user(db, USER_MANAGER):
        hidden = db.execute(
            "select count(*) from cash_receipts where entry_key = 't184-policy'"
        ).fetchone()[0]

    assert seen == 1, "чек не виден тому, кому виден сам расход"
    assert hidden == 0, "чек чужой точки виден управляющему — это утечка суммы и повода"


def test_a_receipt_cannot_be_written_for_an_expense_that_is_not_visible(db):
    """Записать чек к чужому расходу тоже нельзя: политика с `with check`.

    Без этого управляющий не видел бы чужой расход, но мог бы подложить к нему
    свою фотографию — то есть писать в строку, которой для него не существует.
    """
    from facts_helpers import fact_payload, upsert_fact

    upsert_fact(db, fact_payload(unit=U_BG1, key="manual:cash:t184-write"))
    with as_app_user(db, USER_MANAGER) as conn:  # noqa: SIM117
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                """insert into cash_receipts
                       (tenant_id, entry_key, media_type, byte_size, content, sha256)
                   values (%s, 't184-write', 'image/png', %s, %s, 'x')""",
                [T1, len(PNG), PNG],
            )


def test_a_receipt_of_a_visible_expense_may_be_written(db):
    """Парная к предыдущей: своя точка — чек пишется.

    Без неё запрет был бы неотличим от «писать в эту таблицу нельзя никому», и
    зелёная проверка выше ничего бы не значила.
    """
    from facts_helpers import fact_payload, upsert_fact

    upsert_fact(db, fact_payload(unit=U_NS1, key="manual:cash:t184-own"))
    with as_app_user(db, USER_MANAGER):
        db.execute(
            """insert into cash_receipts
                   (tenant_id, entry_key, media_type, byte_size, content, sha256)
               values (%s, 't184-own', 'image/png', %s, %s, 'x')""",
            [T1, len(PNG), PNG],
        )
        assert db.execute(
            "select count(*) from cash_receipts where entry_key = 't184-own'"
        ).fetchone()[0] == 1


def test_the_receipt_may_come_with_the_expense_itself(client, sql):
    """Чек снимается тем же движением, что вносится расход.

    Ради этого он и стоит в форме внесения: управляющий отдаёт деньги, стоя у
    печи, и второго захода «откройте расход и приложите фотографию» не будет
    никогда. Проверяется именно путь формы, а не отдельная ручка: до T184 чек
    можно было бы приложить только вторым действием — то есть почти никогда.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    login_as(client, "admin")
    tenant = sql.execute("select id from tenants order by code limit 1").fetchone()[0]
    unit = sql.execute(
        "select id from units where tenant_id = %s order by code limit 1", [tenant]
    ).fetchone()[0]
    line = sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    item = sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 't184-phone', %s, %s, '2020-01-01') returning id""",
        [tenant, Jsonb({"ru": "С телефона"}), line],
    ).fetchone()[0]
    try:
        answer = client.post("/expenses/new/", {
            "date": "2026-02-21", "amount": "90", "item": str(item),
            "unit": str(unit), "note": "T184 телефон", "entry_key": "",
            "receipt": SimpleUploadedFile("cek.png", PNG, content_type="image/png"),
        })
        assert answer.status_code == 302, body(answer)
        fact = sql.execute(
            """select id from facts
                where note = 'T184 телефон' and superseded_at is null limit 1"""
        ).fetchone()[0]
        assert "Чек приложен" in body(client.get(f"/expenses/{fact}/"))
    finally:
        sql.execute("delete from cash_receipts where entry_key in ("
                    "select replace(dedup_key, 'manual:cash:', '') from facts "
                    "where note = 'T184 телефон')")
        sql.execute("delete from facts where note = 'T184 телефон'")
        sql.execute("delete from expense_items where code = 't184-phone'")
