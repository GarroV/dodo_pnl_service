"""Поведение схемы фактов: идемпотентность, версии, разнесение, закрытый месяц.

Проверки идут владельцем схемы там, где речь про инварианты данных, и ролью
`app_user` там, где речь про доступ (`test_facts_access.py`). Разнесение и
идемпотентность — инварианты: они обязаны держаться независимо от того, кто
пишет.

Числа выбраны так, чтобы ловить именно то, о чём тест: 100.01 на три точки —
сумма, которая делится с остатком, и на двух точках распределение копеек не
проявляется вовсе.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from conftest import (
    CP_EPS,
    I_FOOD,
    I_REVENUE,
    I_TOTAL,
    I_TRANSFER,
    JULY,
    JUNE,
    LE1,
    LE2,
    T1,
    U_BG1,
    U_NS1,
    U_NS2,
    USER_DIRECTOR,
    as_app_user,
)
from facts_helpers import active_facts, fact_payload, upsert_fact


def rule(conn, *, method: str = "even", counterparty: str = CP_EPS, unit: str | None = None,
         valid_from: str = "2026-01-01", valid_to: str | None = None,
         ledger: str = "official") -> str:
    """Правило разнесения расхода по точкам."""
    return conn.execute(
        """insert into allocation_rules
               (tenant_id, counterparty_id, pnl_item_id, method, unit_id, ledger,
                valid_from, valid_to)
           values (%s, %s, %s, %s, %s, %s, %s, %s) returning id""",
        (T1, counterparty, I_FOOD, method, unit, ledger, valid_from, valid_to),
    ).fetchone()[0]


def pending_invoice(conn, *, amount: str = "100.01", legal_entity: str | None = None,
                    key: str = "invoice") -> str:
    """Фактура на юрлицо: точка неизвестна, ждёт правила."""
    fact_id, _ = upsert_fact(
        conn,
        fact_payload(unit=None, allocation="pending", counterparty=CP_EPS,
                     legal_entity=legal_entity, amount=amount, key=key),
    )
    return fact_id


# --- идемпотентность ---------------------------------------------------------

def test_same_event_loaded_twice_creates_one_row(db):
    """Повторная выгрузка того же события не плодит версий."""
    first_id, first = upsert_fact(db, fact_payload(unit=U_BG1, key="line-1"))
    second_id, second = upsert_fact(db, fact_payload(unit=U_BG1, key="line-1"))

    assert first == "inserted"
    assert second == "unchanged"
    assert first_id == second_id
    assert len(active_facts(db, key="line-1")) == 1


def test_changed_amount_replaces_the_version(db):
    """Изменившаяся сумма — новая версия рядом, а не правка на месте.

    Старая строка остаётся в истории со ссылкой на заменившую: закрытый месяц
    обязан объясняться теми числами, которые в нём были.
    """
    old_id, _ = upsert_fact(db, fact_payload(unit=U_BG1, amount="100.00", key="line-1"))
    new_id, action = upsert_fact(db, fact_payload(unit=U_BG1, amount="120.00", key="line-1"))

    assert action == "updated"
    assert new_id != old_id
    assert active_facts(db, key="line-1") == [("line-1", U_BG1, 120, "direct", 2)]

    superseded_at, superseded_by = db.execute(
        "select superseded_at, superseded_by from facts where id = %s", (old_id,)
    ).fetchone()
    assert superseded_at is not None
    assert str(superseded_by) == str(new_id)


def test_document_is_upserted_by_external_id(db):
    """Повторная загрузка документа не плодит строк — идемпотентность на (тенант, источник, id)."""
    payload = {
        "tenant_id": T1, "kind": "invoice", "source": "einvoice",
        "external_id": "RS-2026-1", "doc_date": JUNE, "period": JUNE,
        "total_amount": "100.01",
    }
    from psycopg.types.json import Jsonb

    first = db.execute("select upsert_document(%s)", (Jsonb(payload),)).fetchone()[0]
    second = db.execute("select upsert_document(%s)", (Jsonb(payload),)).fetchone()[0]

    assert first == second
    assert db.execute("select count(*) from source_documents").fetchone()[0] == 1


# --- разнесение по точкам ----------------------------------------------------

def test_even_allocation_matches_to_the_kopeck(db):
    """Сумма детей равна родителю до копейки, а остаток распределён детерминированно."""
    rule(db)
    fact_id = pending_invoice(db, amount="100.01")

    assert db.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 3

    children = db.execute(
        """select u.code, f.amount from facts f join units u on u.id = f.unit_id
            where f.parent_fact_id = %s and f.superseded_at is null order by u.code""",
        (fact_id,),
    ).fetchall()
    assert children == [("BG1", Decimal("33.34")), ("NS1", Decimal("33.33")),
                        ("NS2", Decimal("33.34"))]
    assert sum(amount for _, amount in children) == Decimal("100.01")

    parent = db.execute("select allocation from facts where id = %s", (fact_id,)).fetchone()[0]
    assert parent == "split", "родитель обязан выйти из счёта, иначе двойной счёт"


def test_split_parent_is_not_counted_twice(db):
    """Разнесённая фактура попадает в P&L один раз — детьми."""
    rule(db)
    fact_id = pending_invoice(db, amount="100.01")
    db.execute("select allocate_fact(%s)", (fact_id,))

    with as_app_user(db, USER_DIRECTOR) as conn:
        network = conn.execute(
            "select coalesce(sum(amount), 0) from pnl_by_network"
        ).fetchone()[0]
    assert network == Decimal("100.01")


def test_allocation_is_idempotent(db):
    """Пересчёт на неизменившихся правилах не переписывает факты вслепую."""
    rule(db)
    fact_id = pending_invoice(db)
    db.execute("select allocate_fact(%s)", (fact_id,))

    changed = db.execute("select reallocate_period(%s, %s)", (T1, JUNE)).fetchone()[0]
    assert changed == 0
    assert len(active_facts(db, key="invoice#")) == 3


def test_rule_change_replaces_children(db):
    """Правило поменялось — дети пересобираются, старые уходят в историю."""
    rule(db, method="even", valid_to=JUNE)
    rule(db, method="fixed_unit", unit=U_NS1, valid_from=JUNE)
    fact_id = pending_invoice(db, amount="99.00")
    db.execute("select allocate_fact(%s)", (fact_id,))

    children = active_facts(db, key="invoice#")
    assert [row[1] for row in children] == [U_NS1]
    assert children[0][2] == 99


def test_allocation_by_revenue_follows_the_money(db):
    """Пропорционально выручке: точка без выручки доли не получает."""
    upsert_fact(db, fact_payload(unit=U_BG1, item=I_REVENUE, amount="300.00", key="rev-bg1"))
    upsert_fact(db, fact_payload(unit=U_NS1, item=I_REVENUE, amount="100.00", key="rev-ns1"))
    rule(db, method="by_revenue")
    fact_id = pending_invoice(db, amount="80.00")

    assert db.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 2

    children = db.execute(
        """select u.code, f.amount from facts f join units u on u.id = f.unit_id
            where f.parent_fact_id = %s and f.superseded_at is null order by u.code""",
        (fact_id,),
    ).fetchall()
    assert children == [("BG1", 60), ("NS1", 20)]


def test_allocation_stays_inside_the_legal_entity(db):
    """Фактура пришла на юрлицо — разносится только на его точки."""
    db.execute("update units set legal_entity_id = %s where id = %s", (LE2, U_NS2))
    rule(db)
    fact_id = pending_invoice(db, amount="100.00", legal_entity=LE1)
    db.execute("select allocate_fact(%s)", (fact_id,))

    units = {row[1] for row in active_facts(db, key="invoice#")}
    assert units == {U_BG1, U_NS1}


# --- нераспределённое не исчезает --------------------------------------------

def test_fact_without_a_rule_stays_pending(db):
    """Правила нет — факт ждёт человека и виден в списке нераспределённых."""
    fact_id = pending_invoice(db, amount="50.00")

    assert db.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 0
    assert db.execute(
        "select count(*) from facts_unallocated where fact_id = %s", (fact_id,)
    ).fetchone()[0] == 1


def test_rule_asking_for_a_human_leaves_the_fact_waiting(db):
    """Метод `ask` — это «спросить человека», а не «разнести молча как-нибудь»."""
    rule(db, method="ask")
    fact_id = pending_invoice(db, amount="50.00")

    assert db.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 0
    assert db.execute(
        "select allocation from facts where id = %s", (fact_id,)
    ).fetchone()[0] == "pending"


def test_disappearing_rule_returns_children_to_waiting(db):
    """Правило убрали — дети снимаются, а сумма возвращается в ожидание, не теряется."""
    rule_id = rule(db)
    fact_id = pending_invoice(db, amount="90.00")
    db.execute("select allocate_fact(%s)", (fact_id,))

    db.execute("delete from allocation_rules where id = %s", (rule_id,))
    db.execute("select reallocate_period(%s, %s)", (T1, JUNE))

    assert db.execute(
        "select allocation from facts where id = %s", (fact_id,)
    ).fetchone()[0] == "pending"
    assert active_facts(db, key="invoice#") == []
    assert db.execute(
        "select count(*) from facts_unallocated where fact_id = %s", (fact_id,)
    ).fetchone()[0] == 1


# --- закрытый месяц ----------------------------------------------------------

def test_closed_period_rejects_new_facts(db):
    """Закрытый месяц не меняется ни импортом, ни ручным вводом."""
    db.execute(
        "insert into periods (tenant_id, period, status) values (%s, %s, 'closed')",
        (T1, JULY),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="закрыт"):
        with db.transaction():
            upsert_fact(db, fact_payload(unit=U_BG1, period=JULY, key="late"))


def test_closed_period_rejects_reallocation(db):
    """Пересчёт разнесения в закрытом месяце отказывает вслух, а не молчит.

    Молчаливый пропуск читался бы как «пересчитано»: хуже отказа, потому что о
    нём никто не узнает.
    """
    db.execute(
        "insert into periods (tenant_id, period, status) values (%s, %s, 'closed')",
        (T1, JULY),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="закрыт"):
        with db.transaction():
            db.execute("select reallocate_period(%s, %s)", (T1, JULY))


def test_open_period_accepts_facts(db):
    """Страховка от проверки, которая краснеет всегда: в открытый месяц пишется."""
    _, action = upsert_fact(db, fact_payload(unit=U_BG1, period=JUNE, key="in-time"))
    assert action == "inserted"


# --- ограничения строки ------------------------------------------------------

def test_subtotal_item_takes_no_facts(db):
    """Подытог считается из детей: факт в нём дал бы сумму, не сходящуюся ни с чем."""
    with pytest.raises(psycopg.errors.RaiseException, match="подытог"):
        with db.transaction():
            upsert_fact(db, fact_payload(unit=U_BG1, item=I_TOTAL, key="into-subtotal"))


def test_direct_fact_must_know_its_unit(db):
    """Расход на юрлицо целиком — это `pending` плюс правило, а не факт без точки.

    Иначе суммы «по точкам» и «по сети» разъезжаются молча.
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            upsert_fact(db, fact_payload(unit=None, allocation="direct", key="nowhere"))


def test_period_is_always_the_first_of_the_month(db):
    """Отчёт строится по месяцу: дата в середине месяца — тихо неверный период."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            upsert_fact(db, fact_payload(unit=U_BG1, period="2026-06-15", key="mid-month"))


# --- сборка P&L --------------------------------------------------------------

def test_transfers_do_not_reach_the_report(db):
    """Перевод из кассы в банк — не расход и не выручка, но храним для сверки наличных."""
    upsert_fact(db, fact_payload(unit=U_BG1, item=I_FOOD, amount="10.00", key="expense"))
    upsert_fact(db, fact_payload(unit=U_BG1, item=I_TRANSFER, amount="500.00", key="transfer"))

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select count(*) from facts").fetchone()[0] == 2
        total = conn.execute("select coalesce(sum(amount), 0) from pnl_by_unit").fetchone()[0]
    assert total == 10


def test_network_equals_the_sum_of_units(db):
    """Суммы по точкам и по сети сходятся — иначе отчёт врёт в одном из двух видов."""
    upsert_fact(db, fact_payload(unit=U_BG1, amount="10.00", key="a"))
    upsert_fact(db, fact_payload(unit=U_NS1, amount="20.00", key="b"))
    upsert_fact(db, fact_payload(unit=U_NS2, amount="30.50", key="c"))

    with as_app_user(db, USER_DIRECTOR) as conn:
        by_unit = conn.execute("select sum(amount) from pnl_by_unit").fetchone()[0]
        by_network = conn.execute("select sum(amount) from pnl_by_network").fetchone()[0]
    assert by_unit == by_network == Decimal("60.50")


def test_report_tree_rolls_children_into_the_subtotal(db):
    """Подытог собирает своих потомков: на этом стоит весь отчёт."""
    db.execute("update pnl_items set parent_id = %s where id in (%s, %s)",
               (I_TOTAL, I_FOOD, I_REVENUE))
    upsert_fact(db, fact_payload(unit=U_BG1, item=I_REVENUE, amount="1000.00", key="rev"))
    upsert_fact(db, fact_payload(unit=U_BG1, item=I_FOOD, amount="400.00", key="cost"))

    rows = dict(
        db.execute(
            "select code, signed_amount from pnl_report(%s, %s)", (T1, JUNE)
        ).fetchall()
    )
    assert rows["total"] == 600     # выручка минус расход
    assert rows["revenue"] == 1000
    assert rows["food_cost"] == -400


def test_report_amounts_are_pinned_to_the_rate(db):
    """Курс приколачивается к факту, чтобы закрытый месяц не поехал при обновлении справочника."""
    fact_id, _ = upsert_fact(db, fact_payload(unit=U_BG1, amount="1000.00", key="fx"))

    assert db.execute("select fill_report_amounts(%s, %s)", (T1, JUNE)).fetchone()[0] >= 1

    amount_report, rate, currency = db.execute(
        "select amount_report, fx_rate, report_currency from facts where id = %s", (fact_id,)
    ).fetchone()
    assert currency == "EUR"
    assert rate == Decimal("0.00854")
    assert amount_report == Decimal("8.54")


def test_report_amount_is_computed_on_the_fly_when_not_pinned(db):
    """Пока курс не приколочен, отчёт считает по курсу конца периода, а не молчит нулём."""
    upsert_fact(db, fact_payload(unit=U_BG1, amount="1000.00", key="fx"))

    with as_app_user(db, USER_DIRECTOR) as conn:
        amount_report = conn.execute(
            "select amount_report from pnl_lines where amount = 1000"
        ).fetchone()[0]
    assert amount_report == Decimal("8.54")
