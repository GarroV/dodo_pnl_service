"""Кому видны счета, платежи и неразобранные строки (T151, T152).

Всё гоняется **ролью `app_user`**. Владелец таблиц обходит `force row level
security`, а суперпользователь обходит её всегда: тот же набор проверок,
выполненный владельцем, был бы зелёным при снятых политиках. На этом проекте так
уже прожил незамеченным дефект видимости регистров.

Проверяется не «политики написаны», а три утверждения о деньгах, каждое из
которых легко сломать одной строкой:

1. счёт и его платёж закрыты теми же политиками, что расход, — по точке и по
   регистру, потому что это те же самые `facts`;
2. платёж **не** попадает в P&L: расход уже признан счётом, и второй раз его
   считать нельзя;
3. строка без статьи **видна числом** в P&L по сети — она не исчезает и не
   прячется, иначе дыра в отчёте не кричит.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from conftest import (
    CP_EPS,
    I_FOOD,
    JUNE,
    T1,
    U_BG1,
    U_NS1,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
)
from facts_helpers import fact_payload, upsert_fact

INVOICE = "manual:invoice:"
PAYMENT = "manual:payment:"


def shared(conn, code: str) -> str:
    """Служебная строка P&L, заведённая миграцией `0243`."""
    return str(conn.execute(
        "select id from pnl_items where tenant_id is null and code = %s", (code,)
    ).fetchone()[0])


def document(conn, external: str = "inv-1") -> str:
    return str(conn.execute(
        """insert into source_documents
                (tenant_id, counterparty_id, kind, source, external_id, doc_date, period)
           values (%s, %s, 'invoice', 'manual', %s, '2026-07-03', %s) returning id""",
        (T1, CP_EPS, external, JUNE),
    ).fetchone()[0])


def invoice_line(conn, *, unit=None, ledger="official", amount="24000.00",
                 key="one", item=None, doc=None):
    return upsert_fact(conn, fact_payload(
        unit=unit, ledger=ledger, amount=amount, key=INVOICE + key,
        item=item or I_FOOD, counterparty=CP_EPS, document=doc or document(conn, key),
        doc_date="2026-07-03",
    ))


def payment_line(conn, *, doc, unit=None, ledger="official", amount="24000.00",
                 key="one"):
    return upsert_fact(conn, fact_payload(
        unit=unit, ledger=ledger, amount=amount, key=PAYMENT + key,
        item=shared(conn, "supplier_payment"), counterparty=CP_EPS, document=doc,
        doc_date="2026-08-04", channel="bank",
    ))


# --- точка --------------------------------------------------------------------


def test_the_manager_sees_only_the_invoices_of_his_unit(db):
    """Счёт чужой точки не виден управляющему ни строкой, ни в сумме."""
    invoice_line(db, unit=U_NS1, key="mine", amount="100.00")
    invoice_line(db, unit=U_BG1, key="alien", amount="777.00")

    with as_app_user(db, USER_MANAGER) as conn:
        keys = {
            row[0] for row in conn.execute(
                "select dedup_key from facts where dedup_key like %s", (INVOICE + "%",)
            ).fetchall()
        }
        total = conn.execute(
            "select coalesce(sum(amount), 0) from pnl_lines where counterparty_id = %s",
            (CP_EPS,),
        ).fetchone()[0]
    assert keys == {INVOICE + "mine"}
    assert total == Decimal("100.00")


def test_the_unit_check_is_meaningful(db):
    """Тот же запрос владельцем видит оба счёта — значит выше отсекала RLS."""
    invoice_line(db, unit=U_NS1, key="mine")
    invoice_line(db, unit=U_BG1, key="alien")

    keys = {
        row[0] for row in db.execute(
            "select dedup_key from facts where dedup_key like %s", (INVOICE + "%",)
        ).fetchall()
    }
    assert keys == {INVOICE + "mine", INVOICE + "alien"}


def test_the_manager_cannot_write_an_invoice_for_another_unit(db):
    """Забытый фильтр на записи тоже не проходит: `with check` закрывает вставку."""
    doc = document(db, "alien-doc")
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            invoice_line(conn, unit=U_BG1, key="hack", doc=doc)


def test_the_payment_follows_the_same_unit_rule(db):
    """Платёж — такой же факт: чужая точка не видна и в нём."""
    doc = document(db, "paid")
    invoice_line(db, unit=U_BG1, key="alien", doc=doc)
    payment_line(db, doc=doc, unit=U_BG1, key="alien")

    with as_app_user(db, USER_MANAGER) as conn:
        assert conn.execute(
            "select count(*) from facts where dedup_key like %s", (PAYMENT + "%",)
        ).fetchone()[0] == 0


# --- регистр ------------------------------------------------------------------


def test_an_invisible_ledger_hides_the_invoice_and_its_total(db):
    """Регистр, которого роль не видит, не даёт ни строки, ни следа в итоге (D023)."""
    invoice_line(db, unit=U_NS1, key="open", ledger="official", amount="100.00")
    invoice_line(db, unit=U_NS1, key="hidden", ledger="internal", amount="900.00")

    with as_app_user(db, USER_MANAGER) as conn:      # видит official + supplementary
        keys = {
            row[0] for row in conn.execute(
                "select dedup_key from facts where dedup_key like %s", (INVOICE + "%",)
            ).fetchall()
        }
        total = conn.execute(
            "select coalesce(sum(amount), 0) from pnl_lines where counterparty_id = %s",
            (CP_EPS,),
        ).fetchone()[0]
    assert keys == {INVOICE + "open"}
    assert total == Decimal("100.00")

    with as_app_user(db, USER_ACCOUNTANT) as conn:   # видит все три (D036)
        total = conn.execute(
            "select coalesce(sum(amount), 0) from pnl_lines where counterparty_id = %s",
            (CP_EPS,),
        ).fetchone()[0]
    assert total == Decimal("1000.00")


def test_writing_an_invoice_into_an_invisible_ledger_is_rejected(db):
    doc = document(db, "hidden-doc")
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            invoice_line(conn, unit=U_NS1, ledger="internal", key="hack", doc=doc)


# --- платёж не удваивает расход -----------------------------------------------


def test_the_payment_stays_out_of_pnl(db):
    """Счёт и его оплата — один расход в P&L, а не два."""
    doc = document(db, "one-expense")
    invoice_line(db, unit=U_NS1, key="one", amount="24000.00", doc=doc)
    payment_line(db, doc=doc, unit=U_NS1, key="one", amount="24000.00")

    with as_app_user(db, USER_DIRECTOR) as conn:
        # Сумма ВСЕХ расходных строк периода, а не одной статьи. Проверка по
        # одной статье была бы фиктивной: платёж лежит в своей строке P&L, и
        # под чужим фильтром он не попал бы в число, даже став расходом. Ровно
        # это и показала порча — вид статьи меняли на `expense`, а проверка
        # оставалась зелёной.
        by_network = conn.execute(
            """select coalesce(sum(amount), 0) from pnl_by_network
                where period = %s and kind = 'expense'""",
            (JUNE,),
        ).fetchone()[0]
        # Сам платёж при этом никуда не делся: он есть строкой и виден.
        payments = conn.execute(
            "select count(*) from facts where dedup_key like %s", (PAYMENT + "%",)
        ).fetchone()[0]
    assert by_network == Decimal("24000.00"), "оплата счёта удвоила расход в P&L"
    assert payments == 1


def test_a_payment_without_a_unit_does_not_block_the_month(db):
    """Платёж без точки не стоит в «что мешает закрыть месяц»: в P&L его нет вовсе."""
    doc = document(db, "network")
    invoice_line(db, key="network", doc=doc)                    # счёт на всю сеть
    payment_line(db, doc=doc, key="network")

    with as_app_user(db, USER_DIRECTOR) as conn:
        waiting = {
            row[0] for row in conn.execute(
                "select fact_id::text from facts_unallocated where tenant_id = %s", (T1,)
            ).fetchall()
        }
        payment_id = conn.execute(
            "select id::text from facts where dedup_key = %s", (PAYMENT + "network",)
        ).fetchone()[0]
        invoice_id = conn.execute(
            "select id::text from facts where dedup_key = %s", (INVOICE + "network",)
        ).fetchone()[0]

    # Счёт без точки в списке стоит — он и правда ждёт разнесения.
    assert invoice_id in waiting
    # А платёж нет: разносить его нечего, закрытию месяца он не мешает.
    assert payment_id not in waiting


# --- строка без статьи --------------------------------------------------------


def test_a_line_without_an_article_is_visible_as_a_number(db):
    """Неразобранная сумма считается в P&L по сети, а не пропадает молча."""
    unclassified = shared(db, "unclassified")
    invoice_line(db, unit=U_NS1, key="raw", amount="5000.00", item=unclassified)

    with as_app_user(db, USER_DIRECTOR) as conn:
        shown = conn.execute(
            """select pnl_code, amount from pnl_by_network
                where period = %s and pnl_code = 'unclassified'""",
            (JUNE,),
        ).fetchone()
    assert shown is not None, "неразобранная сумма исчезла из P&L — так нельзя"
    assert shown[1] == Decimal("5000.00")


def test_an_unclassified_line_obeys_the_ledger_rule_too(db):
    """Служебная статья не открывает обхода: регистр по-прежнему решает (D023)."""
    unclassified = shared(db, "unclassified")
    invoice_line(db, unit=U_NS1, key="raw", amount="5000.00", item=unclassified,
                 ledger="internal")

    with as_app_user(db, USER_MANAGER) as conn:
        assert conn.execute(
            """select count(*) from pnl_by_network
                where period = %s and pnl_code = 'unclassified'""",
            (JUNE,),
        ).fetchone()[0] == 0
