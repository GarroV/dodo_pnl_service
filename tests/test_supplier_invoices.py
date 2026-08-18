"""Счёт и платёж — разные события с разными датами (T151).

Главное, что здесь проверяется, одно: **три даты не слиплись**. Дата документа,
период учёта и дата денег живут порознь, и продукт обязан класть счёт в тот
месяц, к которому он относится, а не в тот, когда он пришёл или когда его
оплатили. Это и есть та половина учёта, которой у партнёра сегодня нет.

Рядом — правила, унаследованные от третьей очереди и обязанные работать здесь
так же: закрытый месяц не двигается (D020), повторная отправка формы не заводит
второй строки, чужая точка отвергается базой (D014), а отказ не рассказывает,
существует ли она (D023).

Проверки идут **через экраны**, то есть тем же путём, что и человек. Прямое
чтение базы — только осмотр результата, и идёт оно владельцем схемы: это не
проверка доступа. Доступ проверяется отдельно и ролью `app_user`
(`tests/test_supplier_access.py`).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as
from test_directory import approve_june, payruns_restored, sql  # noqa: F401

INVOICES = "/invoices/"
NEW = "/invoices/new/"
JUNE_DAY = "2026-06-15"
JULY_DAY = "2026-07-03"


def current_period() -> date:
    return date.today().replace(day=1)


@pytest.fixture
def tenant(sql):  # noqa: F811
    return str(sql.execute("select id from tenants where code = 'rs-dev'").fetchone()[0])


@pytest.fixture
def units(sql, tenant):  # noqa: F811
    return {
        code: str(unit_id)
        for unit_id, code in sql.execute(
            "select id, code from units where tenant_id = %s", (tenant,)
        ).fetchall()
    }


@pytest.fixture
def counterparty(sql, tenant):  # noqa: F811
    """Контрагент для счёта. Кладётся владельцем схемы: это подготовка, не проверка."""
    row = sql.execute(
        """insert into counterparties (tenant_id, title, valid_from)
           values (%s, 'EPS Elektro', '2020-01-01') returning id""",
        (tenant,),
    ).fetchone()[0]
    yield str(row)
    sql.execute("delete from counterparties where id = %s", (row,))


@pytest.fixture
def item(sql, tenant):  # noqa: F811
    """Статья расходов. Справочник поставляется пустым (Q015), материал заводит тест."""
    from psycopg.types.json import Jsonb

    pnl = sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    row = sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 'inv-test', %s, %s, '2020-01-01') returning id""",
        (tenant, Jsonb({"ru": "Электричество", "en": "Electricity",
                        "sr-latn": "Struja"}), pnl),
    ).fetchone()[0]
    yield str(row)
    sql.execute("delete from facts where expense_item_id = %s", (row,))
    sql.execute("delete from expense_items where id = %s", (row,))


@pytest.fixture
def invoices_removed(sql, tenant):  # noqa: F811
    """Счета и платежи теста не переживают его.

    Просить эту фикстуру нужно **раньше** `payruns_restored`: разбираются они в
    обратном порядке, а факт закрытого месяца база удалить не даст
    (`facts_guard`), пока месяц не открыт заново. Вместе со строками убираются
    месяцы, которые они за собой завели, — иначе оставленный август молча
    подменял бы июнь соседним тестам, берущим период первой ссылкой со списка.
    """
    before = [
        row[0] for row in sql.execute(
            "select period from periods where tenant_id = %s", (tenant,)
        ).fetchall()
    ]
    yield
    sql.execute(
        "delete from facts where dedup_key like 'manual:invoice:%%' "
        "or dedup_key like 'manual:payment:%%' or dedup_key like 'manual:purchase:%%'"
    )
    sql.execute("delete from source_documents where source = 'manual' and kind = 'invoice'")
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


def key() -> str:
    return str(uuid.uuid4())


def invoice_form(party: str, units: dict, **extra) -> dict:
    fields = {
        "date": JULY_DAY,
        "period": "2026-06",
        "counterparty": party,
        "number": "EPS-77",
        "unit": units["BG1"],
        "ledger": "official",
        "amount": "24000.00",
        "note": "июньское электричество",
        "entry_key": key(),
    }
    fields.update(extra)
    return fields


def lines(sql, prefix: str = "manual:invoice:") -> list[tuple]:  # noqa: F811
    return sql.execute(
        """select dedup_key, amount, period, doc_date, unit_id::text, allocation::text,
                  superseded_at, document_id::text, expense_item_id::text,
                  channel::text, i.code
             from facts f join pnl_items i on i.id = f.pnl_item_id
            where dedup_key like %s order by dedup_key, revision""",
        (f"{prefix}%",),
    ).fetchall()


def june_total(sql) -> object:  # noqa: F811
    """Сумма действующих фактов закрытого месяца — то, что не должно двигаться."""
    return sql.execute(
        "select coalesce(sum(amount), 0) from facts "
        "where period = '2026-06-01' and superseded_at is null"
    ).fetchone()[0]


# --- три даты -----------------------------------------------------------------


def test_the_accounting_period_is_not_the_document_date(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Счёт за июнь, пришедший в июле, ложится в ИЮНЬ — по выбранному периоду."""
    login_as(client, "accountant")
    answer = client.post(NEW, invoice_form(counterparty, units, item=item))
    assert answer.status_code == 302, body(answer)

    (row,) = lines(sql)
    _key, amount, period, doc_date, unit_id, allocation, gone, document, expense, channel, _code = row
    assert period.isoformat() == "2026-06-01"      # период учёта
    assert doc_date.isoformat() == JULY_DAY        # дата документа
    assert amount == Decimal("24000.00")
    assert allocation == "direct"
    assert gone is None
    assert document is not None
    assert expense == item
    # Канала у счёта нет: деньги ещё не двигались. Это и есть разница между
    # обязательством и платежом, выраженная в данных.
    assert channel is None


def test_an_empty_period_falls_back_to_the_month_of_the_document(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Пустой период учёта — месяц документа, а не текущий: выбирать нечего."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item, period=""))

    (row,) = lines(sql)
    assert row[2].isoformat() == "2026-07-01"


def test_the_payment_carries_its_own_date_and_month(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Дата денег — третья дата: у платежа свой месяц, и он не месяц счёта."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item))
    invoice_id = lines(sql)[0][7]
    document = sql.execute(
        "select id from source_documents where id = %s", (invoice_id,)
    ).fetchone()[0]

    paid = client.post(f"/invoices/{document}/pay/", {
        "date": "2026-08-04", "amount": "24000.00", "entry_key": key(),
    })
    assert paid.status_code == 302, body(paid)

    (payment,) = lines(sql, "manual:payment:")
    assert payment[2].isoformat() == "2026-08-01"     # период платежа — месяц денег
    assert payment[3].isoformat() == "2026-08-04"     # дата денег
    assert payment[7] == invoice_id                   # привязан к тому же счёту
    assert payment[9] == "bank"                       # канал: банк, кассы не было
    # Служебная строка P&L, вид `transfer`: расход уже признан счётом, и второй
    # раз в P&L он попасть не должен.
    assert payment[10] == "supplier_payment"

    # Строка счёта при этом не тронута: платёж её не переписывает.
    assert lines(sql)[0][2].isoformat() == "2026-06-01"


def test_the_payment_does_not_double_the_expense_in_pnl(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Счёт и его оплата — это ОДИН расход в P&L, а не два."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item))
    document = lines(sql)[0][7]
    client.post(f"/invoices/{document}/pay/", {
        "date": "2026-06-20", "amount": "24000.00", "entry_key": key(),
    })

    # Считается сумма ВСЕХ расходных строк июня, а не одной статьи: платёж
    # лежит в своей строке P&L, и проверка по чужому коду осталась бы зелёной,
    # даже если бы он в P&L попал.
    spent = sql.execute(
        """select coalesce(sum(amount), 0) from pnl_by_network
            where period = '2026-06-01' and kind = 'expense'"""
    ).fetchone()[0]
    assert spent == Decimal("24000.00"), "оплата счёта удвоила расход в P&L"


# --- оплачено или нет ---------------------------------------------------------


def test_the_list_shows_what_is_left_to_pay(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Остаток считается сложением платежей, а не колонкой «оплачено»."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item))
    document = lines(sql)[0][7]

    shown = body(client.get(INVOICES, {"from": "2026-07-01", "to": "2026-07-31"}))
    assert 'data-left="24000.00"' in shown
    assert 'data-state="unpaid"' in shown

    client.post(f"/invoices/{document}/pay/", {
        "date": "2026-07-20", "amount": "10000.00", "entry_key": key(),
    })
    shown = body(client.get(INVOICES, {"from": "2026-07-01", "to": "2026-07-31"}))
    assert 'data-paid="10000.00"' in shown
    assert 'data-left="14000.00"' in shown
    assert 'data-state="partly"' in shown

    client.post(f"/invoices/{document}/pay/", {
        "date": "2026-07-25", "amount": "14000.00", "entry_key": key(),
    })
    shown = body(client.get(INVOICES, {"from": "2026-07-01", "to": "2026-07-31"}))
    assert 'data-left="0"' in shown
    assert 'data-state="paid"' in shown


def test_the_unpaid_filter_is_a_slice_not_another_schema(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """«Только неоплаченные» — отбор показа: оба варианта Q017 живут рядом."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item, number="EPS-1"))
    client.post(NEW, invoice_form(counterparty, units, item=item, number="EPS-2",
                                  amount="5000.00"))
    first = lines(sql)[0][7]
    client.post(f"/invoices/{first}/pay/", {
        "date": "2026-07-20", "amount": "24000.00", "entry_key": key(),
    })

    window = {"from": "2026-07-01", "to": "2026-07-31"}
    everything = body(client.get(INVOICES, window))
    assert everything.count("data-invoice=") == 2

    unpaid = body(client.get(INVOICES, dict(window, state="unpaid")))
    assert unpaid.count("data-invoice=") == 1
    assert 'data-state="unpaid"' in unpaid

    paid = body(client.get(INVOICES, dict(window, state="paid")))
    assert paid.count("data-invoice=") == 1
    assert 'data-state="paid"' in paid


def test_an_advance_is_a_payment_without_an_invoice(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Оплата без счёта пишется одной строкой и попадает в P&L датой денег."""
    login_as(client, "accountant")
    answer = client.post("/payments/new/", {
        "date": "2026-07-09", "counterparty": counterparty, "item": item,
        "unit": units["NS1"], "ledger": "official", "amount": "3200.00",
        "note": "лампочки", "entry_key": key(),
    })
    assert answer.status_code == 302, body(answer)

    (row,) = lines(sql, "manual:purchase:")
    assert row[2].isoformat() == "2026-07-01"    # период = месяц денег
    assert row[3].isoformat() == "2026-07-09"
    assert row[9] == "bank"
    assert row[10] == "food_cost"                # расход, а не перевод


# --- закрытый месяц -----------------------------------------------------------


def test_an_invoice_for_a_closed_month_lands_beside_it(
    client, web_env, sql, counterparty, item, units,  # noqa: F811
    invoices_removed, payruns_restored,  # noqa: F811
):
    """Июнь закрыт — счёт за июнь ложится в открытый месяц, дата остаётся своей."""
    approve_june(client, web_env)
    before = june_total(sql)

    login_as(client, "accountant")
    answer = client.post(NEW, invoice_form(counterparty, units, item=item), follow=True)
    assert answer.status_code == 200

    (row,) = lines(sql)
    assert row[2] == current_period()             # период учёта — текущий месяц
    assert row[3].isoformat() == JULY_DAY         # дата документа не тронута
    assert june_total(sql) == before              # закрытый месяц не сдвинулся

    # И человеку об этом сказано словами, а не молча.
    assert "закрыт" in body(answer)


def test_editing_an_invoice_of_a_closed_month_goes_by_storno(
    client, web_env, sql, counterparty, item, units,  # noqa: F811
    invoices_removed, payruns_restored,  # noqa: F811
):
    """Правка закрытого месяца — сторно и исправление рядом, D020."""
    login_as(client, "accountant")
    client.post(NEW, invoice_form(counterparty, units, item=item))
    document = lines(sql)[0][7]

    approve_june(client, web_env)
    before = june_total(sql)

    login_as(client, "accountant")
    answer = client.post(f"/invoices/{document}/", invoice_form(
        counterparty, units, item=item, amount="30000.00",
    ), follow=True)
    assert answer.status_code == 200, body(answer)

    assert june_total(sql) == before, "закрытый месяц переписан — это запрещено"

    marks = {row[0].split("#")[-1] if "#" in row[0] else "primary": row
             for row in lines(sql)}
    assert marks["primary"][1] == Decimal("24000.00")     # исходная строка цела
    assert marks["storno"][1] == Decimal("-24000.00")     # сторно отменяет её
    assert marks["fix"][1] == Decimal("30000.00")         # исправление рядом

    # Сторно и исправление лежат в ОДНОМ месяце. Разъехавшись, они дали бы в
    # одном месяце расход из ниоткуда, а в другом — двойной: пара «минус
    # старое, плюс новое» верна только целиком. Считается месяц от периода
    # учёта исходной строки, а не от даты документа, — у счёта это разные
    # месяцы по замыслу.
    assert marks["storno"][2] == marks["fix"][2] == current_period()
    assert marks["primary"][2].isoformat() == "2026-06-01"

    # Счёт остался ОДНИМ счётом: исправление привязано к тому же документу, а не
    # завело второй с тем же номером.
    assert {row[7] for row in lines(sql)} == {document}

    # И в списке он одной строкой с сегодняшней суммой.
    shown = body(client.get(INVOICES, {"from": "2026-07-01", "to": "2026-07-31"}))
    assert shown.count("data-invoice=") == 1
    assert 'data-amount="30000.00"' in shown


# --- повторная отправка -------------------------------------------------------


def test_the_same_form_submitted_twice_writes_one_invoice(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Двойное нажатие «Сохранить» не превращает один счёт в два."""
    login_as(client, "accountant")
    form = invoice_form(counterparty, units, item=item)
    client.post(NEW, form)
    client.post(NEW, form)

    assert len(lines(sql)) == 1
    assert sql.execute(
        "select count(*) from source_documents where source = 'manual' and kind = 'invoice'"
    ).fetchone()[0] == 1


def test_the_same_key_with_other_data_replaces_the_row(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Тот же ключ с другой суммой заводит версию, а не второй счёт."""
    login_as(client, "accountant")
    form = invoice_form(counterparty, units, item=item)
    client.post(NEW, form)
    client.post(NEW, dict(form, amount="26000.00"))

    rows = lines(sql)
    assert len(rows) == 2
    active = [row for row in rows if row[6] is None]
    assert len(active) == 1
    assert active[0][1] == Decimal("26000.00")


# --- отказы -------------------------------------------------------------------


def test_the_manager_cannot_substitute_another_unit(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Чужая точка отвергается БАЗОЙ: представление её не фильтрует (D014)."""
    login_as(client, "manager")          # управляющий NS1
    answer = client.post(NEW, invoice_form(counterparty, units, item=item,
                                           unit=units["BG1"]))
    assert answer.status_code == 400, body(answer)
    assert lines(sql) == []


def test_the_refusal_does_not_reveal_that_the_unit_exists(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Чужая точка и выдуманная отвечают одинаково (D023)."""
    login_as(client, "manager")
    alien = client.post(NEW, invoice_form(counterparty, units, item=item,
                                          unit=units["BG1"]))
    nobody = client.post(NEW, invoice_form(counterparty, units, item=item,
                                           unit=str(uuid.uuid4())))
    assert alien.status_code == nobody.status_code
    assert "Точка не найдена" in body(alien)
    assert "Точка не найдена" in body(nobody)


def test_an_invoice_without_a_counterparty_is_refused(
    client, sql, counterparty, item, units, invoices_removed,  # noqa: F811
):
    """Счёт не на кого — не счёт: смысл справочника в том, чтобы траты складывались."""
    login_as(client, "accountant")
    answer = client.post(NEW, invoice_form(counterparty, units, item=item,
                                           counterparty=""))
    assert answer.status_code == 400
    assert "Контрагент" in body(answer)
    assert lines(sql) == []


def test_a_counterparty_that_does_not_act_on_that_date_is_refused(
    client, sql, tenant, item, units, invoices_removed,  # noqa: F811
):
    """Контрагент, закрытый до даты счёта, не объясняет этот счёт."""
    closed = sql.execute(
        """insert into counterparties (tenant_id, title, valid_from, valid_to)
           values (%s, 'Ушедший', '2020-01-01', '2026-01-01') returning id""",
        (tenant,),
    ).fetchone()[0]
    try:
        login_as(client, "accountant")
        answer = client.post(NEW, invoice_form(str(closed), units, item=item))
        assert answer.status_code == 400
        assert "не действует" in body(answer)
    finally:
        sql.execute("delete from counterparties where id = %s", (closed,))


def test_an_unknown_invoice_answers_404(client, invoices_removed):  # noqa: F811
    login_as(client, "accountant")
    assert client.get(
        "/invoices/00000000-0000-4000-8000-00000000009f/"
    ).status_code == 404
