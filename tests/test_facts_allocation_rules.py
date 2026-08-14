"""Правило разнесения по статье расхода (T111).

Схема разнесения (T107) ищет правило по контрагенту: так приходят фактуры
поставщиков. У расхода, внесённого руками из кассы, контрагента нет и взяться
ему неоткуда — человек выбирает **статью** («аренда офиса», «реклама на сеть»),
и именно она отвечает на вопрос «как это разносить». Поэтому правило получает
второй ключ, и ровно один из двух у него заполнен.

Проверки здесь — про инварианты данных, поэтому идут владельцем схемы; про
доступ к тем же правилам — `test_facts_access.py`, и там роль `app_user`.
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
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
    as_directory_admin,
)
from facts_helpers import fact_payload, upsert_fact


def expense_item(conn, *, code: str = "rent") -> str:
    """Статья расходов — материал теста. Заводится тем, кто вправе вести справочник."""
    from psycopg.types.json import Jsonb

    with as_directory_admin(conn):
        return conn.execute(
            """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
               values (%s, %s, %s, %s, '2020-01-01') returning id""",
            (T1, code, Jsonb({"ru": "Аренда офиса"}), I_FOOD),
        ).fetchone()[0]


def item_rule(conn, item_id, *, method: str = "even", unit: str | None = None,
              valid_from: str = "2026-01-01", valid_to: str | None = None,
              ledger: str = "official") -> str:
    """Правило разнесения по статье расхода."""
    return conn.execute(
        """insert into allocation_rules
               (tenant_id, expense_item_id, pnl_item_id, method, unit_id, ledger,
                valid_from, valid_to)
           values (%s, %s, %s, %s, %s, %s, %s, %s) returning id""",
        (T1, item_id, I_FOOD, method, unit, ledger, valid_from, valid_to),
    ).fetchone()[0]


def waiting_expense(conn, item_id, *, amount: str = "100.01", key: str = "cash",
                    ledger: str = "official") -> str:
    """Расход без точки: внесён на всю сеть и ждёт правила."""
    payload = fact_payload(
        unit=None, allocation="pending", amount=amount, key=key, ledger=ledger,
    )
    payload["expense_item_id"] = str(item_id)
    fact_id, _ = upsert_fact(conn, payload)
    return fact_id


def children_of(conn, fact_id) -> list[tuple]:
    return conn.execute(
        """select u.code, f.amount, f.expense_item_id::text
             from facts f join units u on u.id = f.unit_id
            where f.parent_fact_id = %s and f.superseded_at is null
            order by u.code""",
        (fact_id,),
    ).fetchall()


# --- правило по статье --------------------------------------------------------

def test_a_rule_by_expense_item_splits_the_expense(db):
    """Расход без точки со статьёй разносится правилом этой статьи до копейки."""
    item_id = expense_item(db)
    item_rule(db, item_id)
    fact_id = waiting_expense(db, item_id, amount="100.01")

    assert db.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 3

    children = children_of(db, fact_id)
    assert [(code, amount) for code, amount, _ in children] == [
        ("BG1", Decimal("33.34")), ("NS1", Decimal("33.33")), ("NS2", Decimal("33.34")),
    ]
    assert sum(amount for _, amount, _ in children) == Decimal("100.01")


def test_the_children_keep_the_expense_item(db):
    """Статья не теряется при разнесении.

    Дефект, найденный этой задачей: `allocate_fact` собирал ребёнка без
    `expense_item_id`, потому что колонка появилась позже самой функции (T108).
    Разнесённый расход становился безымянным в списке расходов и переставал
    находиться фильтром по статье — молча, потому что строка P&L при этом
    оставалась на месте и отчёт сходился.
    """
    item_id = expense_item(db)
    item_rule(db, item_id)
    fact_id = waiting_expense(db, item_id)
    db.execute("select allocate_fact(%s)", (fact_id,))

    assert [item for _, _, item in children_of(db, fact_id)] == [str(item_id)] * 3


def test_a_rule_of_another_item_does_not_apply(db):
    """Правило соседней статьи чужой расход не разносит: ключ — это ключ."""
    mine = expense_item(db, code="rent")
    other = expense_item(db, code="ads")
    item_rule(db, other)
    fact_id = waiting_expense(db, mine)

    assert db.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 0
    assert db.execute(
        "select allocation from facts where id = %s", (fact_id,)
    ).fetchone()[0] == "pending"


def test_the_expense_stays_visible_while_it_waits(db):
    """Расход без правила виден в списке нераспределённого, а не исчезает."""
    item_id = expense_item(db)
    fact_id = waiting_expense(db, item_id, amount="500.00")

    assert db.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 0
    waiting = db.execute(
        "select fact_id::text, amount from facts_unallocated where tenant_id = %s", (T1,)
    ).fetchall()
    assert waiting == [(str(fact_id), Decimal("500.00"))]


def test_the_rule_follows_the_ledger(db):
    """У статьи законно два правила — по одному на регистр, и выбирается своё.

    Та же причина, что у правил по контрагенту (T107): одна и та же трата
    бывает и официальной, и из кассы, и разносится она по-разному.
    """
    item_id = expense_item(db)
    item_rule(db, item_id, method="fixed_unit", unit=U_BG1, ledger="internal")
    item_rule(db, item_id, method="even", ledger="official")

    fact_id = waiting_expense(db, item_id, ledger="internal", key="internal-cash")
    db.execute("select allocate_fact(%s)", (fact_id,))
    assert [code for code, _, _ in children_of(db, fact_id)] == ["BG1"]


def test_recalculation_on_unchanged_rules_changes_nothing(db):
    """Пересчёт не переписывает факты вслепую: ноль изменений."""
    item_id = expense_item(db)
    item_rule(db, item_id)
    fact_id = waiting_expense(db, item_id)
    db.execute("select allocate_fact(%s)", (fact_id,))
    before = db.execute(
        "select id, revision from facts where superseded_at is null order by id"
    ).fetchall()

    assert db.execute("select reallocate_period(%s, %s)", (T1, JUNE)).fetchone()[0] == 0
    assert db.execute(
        "select id, revision from facts where superseded_at is null order by id"
    ).fetchall() == before


# --- кто вправе разносить -----------------------------------------------------

def test_a_role_limited_to_its_own_unit_cannot_allocate(db):
    """Разносит тот, кто ведёт все точки партнёра, — и отвергает база.

    Дело не в правах, а в верности плана: `allocation_plan` читает `units` под
    политиками того, кто её позвал. У управляющего список точек короче, и без
    этого отказа разнесение не сломалось бы, а **тихо** положило бы всю сумму
    сети на его единственную точку. Два человека, нажавших одну и ту же кнопку,
    получали бы разные числа и не узнали бы об этом.

    **Чем именно отвергается — с T130 другим рубежом.** Разносится всегда факт
    без точки (`pending` без точки — требование схемы), а такой факт роли с
    урезанным набором точек больше не виден вовсе: она получает «факт не
    найден» ещё до охраны разнесения. Охрана осталась и снята не будет — она
    вторая линия и единственная, что удержит вызов, если завтра видимость
    сузят иначе. Поэтому здесь проверяется исход, общий для обоих рубежей:
    отказ и ни одной строки на чужих точках.
    """
    item_id = expense_item(db)
    item_rule(db, item_id)
    fact_id = waiting_expense(db, item_id, amount="100.01")

    with as_app_user(db, USER_MANAGER) as conn:
        # Точка сохранения, а не откат всей транзакции: после отката проверять
        # было бы нечего — материал теста уехал бы вместе с отказом.
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute("select allocate_fact(%s)", (fact_id,))

    assert children_of(db, fact_id) == [], "расход разошёлся по чужим точкам"
    assert db.execute(
        "select allocation from facts where id = %s", (fact_id,)
    ).fetchone()[0] == "pending"


def test_a_role_with_every_unit_allocates(db):
    """Отказ выше — не «никто не может»: тот, кто ведёт все точки, разносит.

    Проверяется ролью `app_user`, а не владельцем схемы: под владельцем политики
    не действуют, и зелёный результат ничего не значил бы.
    """
    item_id = expense_item(db)
    item_rule(db, item_id)
    fact_id = waiting_expense(db, item_id, amount="100.01")

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 3


# --- инварианты правила -------------------------------------------------------

def test_a_rule_has_exactly_one_key(db):
    """Правило знает либо контрагента, либо статью — но не оба и не ничего."""
    item_id = expense_item(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            """insert into allocation_rules
                   (tenant_id, counterparty_id, expense_item_id, pnl_item_id, method,
                    valid_from)
               values (%s, %s, %s, %s, 'even', '2026-01-01')""",
            (T1, CP_EPS, item_id, I_FOOD),
        )
    db.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            """insert into allocation_rules (tenant_id, pnl_item_id, method, valid_from)
               values (%s, %s, 'even', '2026-01-01')""",
            (T1, I_FOOD),
        )


def test_two_rules_of_one_item_cannot_overlap(db):
    """Двух ответов на вопрос «как разносить аренду в марте» быть не должно."""
    item_id = expense_item(db)
    item_rule(db, item_id, valid_from="2026-01-01")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        item_rule(db, item_id, valid_from="2026-03-01", method="by_revenue")


def test_versions_that_do_not_overlap_live_together(db):
    """Правило версионируется по датам: закрытый период не ломается сменой правила."""
    item_id = expense_item(db)
    item_rule(db, item_id, valid_from="2026-01-01", valid_to="2026-07-01")
    item_rule(db, item_id, valid_from="2026-07-01", method="by_revenue")

    assert db.execute(
        "select count(*) from allocation_rules where expense_item_id = %s", (item_id,)
    ).fetchone()[0] == 2
