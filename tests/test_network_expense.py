"""Расход на всю сеть: кто его видит и что с ним делает снятие (T130, T131).

Два дефекта седьмой сверки, оба про деньги и оба воспроизведены на раскатанном
продукте.

**T130.** Расход юрлица целиком (`unit_id is null`) был виден и правился любым,
кто работает у партнёра, — включая управляющего одной точки. Проверки видимости
живут в `test_facts_access.py`; здесь остаётся то, что относится к самому
расходу на сеть: его дети после починки обязаны остаться видимыми своей точке.

**T131.** `supersede_fact` снимала детей обычной выборкой, то есть **под
политиками вызвавшего**. Роль с урезанным набором точек снимала только своего
ребёнка, а остальные оставались живыми в `pnl_lines` при удалённом родителе:
66,68 денег, чей источник помечен удалённым и которых не найти ни на одном
экране (дети из списка расходов исключены).

Проверки идут **ролью `app_user`**: под владельцем схемы политики не действуют,
и весь этот файл был бы зелёным при снятых политиках — то есть бессмысленным.
Итог считается **числом** (сумма по строкам P&L до и после), а не количеством
строк: половина суммы при верном числе строк — ровно то, что здесь ловится.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg

from conftest import (
    JUNE,
    T1,
    U_NS1,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
)
from facts_helpers import fact_payload, upsert_fact
from test_facts_allocation_rules import expense_item, item_rule, waiting_expense

# Сумма, которая не делится на три ровно: копейка обязана куда-то лечь, и
# половинчатое снятие на ней видно числом (33,33 + 33,34 + 33,34 = 100,01).
NETWORK_AMOUNT = "100.01"


def spread_expense(db) -> str:
    """Расход на всю сеть, разнесённый по трём точкам правилом статьи."""
    item_id = expense_item(db)
    item_rule(db, item_id)
    fact_id = waiting_expense(db, item_id, amount=NETWORK_AMOUNT)
    assert db.execute("select allocate_fact(%s)", (fact_id,)).fetchone()[0] == 3
    return fact_id


def alive_in_pnl(conn, fact_id) -> tuple[int, Decimal]:
    """Сколько строк P&L и на какую сумму осталось от этого расхода.

    `pnl_lines` — то самое представление, из которого собирается отчёт, и
    смотреть надо именно в него: `split`-родитель из него исключён, поэтому
    после разнесения там лежат ровно дети. Ссылки на родителя представление не
    отдаёт, поэтому дети выбираются из `facts` — под теми же политиками, что и
    само представление, то есть срез роли остаётся честным.
    """
    rows, total = conn.execute(
        """select count(*), coalesce(sum(amount), 0) from pnl_lines
            where fact_id in (select id from facts where parent_fact_id = %s)""",
        (fact_id,),
    ).fetchone()
    return rows, total


# --- T131: снятие родителя снимает всех детей ---------------------------------


def test_removing_a_network_expense_takes_every_child_with_it(db):
    """Родителя сняли — в P&L не осталось ни копейки его денег."""
    fact_id = spread_expense(db)

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert alive_in_pnl(conn, fact_id) == (3, Decimal(NETWORK_AMOUNT))
        conn.execute("select supersede_fact(%s)", (fact_id,))
        assert alive_in_pnl(conn, fact_id) == (0, Decimal("0"))


def test_the_sweep_does_not_depend_on_who_sees_what(db):
    """Роль, которой видна часть детей, снимает либо всех, либо никого.

    Ровно этот дефект: управляющий NS1 удалил родителя 100,01 — снялся его
    ребёнок 33,33, а 33,34 и 33,34 остались живыми. Половинчатого исхода быть
    не должно, поэтому здесь отказ; проверяется он **числом**, а не текстом
    ошибки: главное — что после отказа в P&L лежит ровно то же, что лежало.
    """
    fact_id = spread_expense(db)

    with as_app_user(db, USER_MANAGER) as conn:
        refused = False
        try:
            with conn.transaction():
                conn.execute("select supersede_fact(%s)", (fact_id,))
        except psycopg.errors.InsufficientPrivilege:
            refused = True

    # Сначала деньги: без починки здесь 2 строки на 66,68 — те самые две трети,
    # чей источник помечен удалённым и которых не найти ни на одном экране.
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert alive_in_pnl(conn, fact_id) == (3, Decimal(NETWORK_AMOUNT))
    assert refused, "снятие прошло молча, а должно было отказать целиком"


def test_a_plain_expense_is_still_removed_by_its_own_unit(db):
    """Отказ выше — не «управляющий больше ничего не удаляет».

    Свой расход своей точки он снимает как раньше: детей у такой строки нет,
    и снимать по чужим точкам нечего.
    """
    fact_id, _ = upsert_fact(db, fact_payload(unit=U_NS1, amount="50.55", key="mine"))

    with as_app_user(db, USER_MANAGER) as conn:
        conn.execute("select supersede_fact(%s)", (fact_id,))
        assert conn.execute(
            "select count(*) from facts where id = %s and superseded_at is null",
            (fact_id,),
        ).fetchone()[0] == 0


def test_replacing_a_network_expense_takes_the_old_children_with_it(db):
    """Та же полнота на замене версии, а не только на удалении.

    Правка расхода идёт `upsert_fact` тем же ключом, а тот зовёт
    `supersede_fact` — то есть дети старой версии снимаются тем же путём. Без
    этого рядом с новой суммой продолжали бы лежать доли старой: двойной счёт.
    """
    fact_id = spread_expense(db)

    with as_app_user(db, USER_DIRECTOR) as conn:
        conn.execute(
            "select upsert_fact(%s)",
            (_changed_payload(conn, fact_id),),
        )
        assert alive_in_pnl(conn, fact_id) == (0, Decimal("0"))
        # Новая версия при этом на месте и ждёт разнесения, а не потерялась.
        assert conn.execute(
            """select amount, allocation::text from facts
                where dedup_key = 'cash' and superseded_at is null""",
        ).fetchone() == (Decimal("70.00"), "pending")


def _changed_payload(conn, fact_id):
    """Тот же расход другой суммой: ключ записи прежний, значит это правка."""
    from psycopg.types.json import Jsonb

    row = conn.execute(
        """select period::text, pnl_item_id::text, expense_item_id::text,
                  ledger::text, currency, title, source::text, dedup_key
             from facts where id = %s""",
        (fact_id,),
    ).fetchone()
    period, pnl_item, expense_item_id, ledger, currency, title, source, key = row
    return Jsonb({
        "tenant_id": T1,
        "period": period,
        "pnl_item_id": pnl_item,
        "expense_item_id": expense_item_id,
        "ledger": ledger,
        "currency": currency,
        "title": title,
        "source": source,
        "dedup_key": key,
        "amount": "70.00",
        "allocation": "pending",
    })


# --- T130: дети сетевого расхода остаются при своей точке ---------------------


def test_the_child_of_a_network_expense_stays_visible_to_its_unit(db):
    """Починка видимости не должна была задеть разнесённые доли.

    Родителя управляющий не видит — это трата юрлица. А ребёнок разнесения —
    уже строка его точки: она входит в P&L NS1, и спрятать её значило бы
    недосчитать точке её же расходов.
    """
    fact_id = spread_expense(db)

    with as_app_user(db, USER_MANAGER) as conn:
        assert conn.execute(
            "select count(*) from facts where id = %s", (fact_id,)
        ).fetchone()[0] == 0
        assert alive_in_pnl(conn, fact_id) == (1, Decimal("33.33"))

    # Контроль: у того, кто ведёт все точки, по-прежнему все три доли.
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        assert alive_in_pnl(conn, fact_id) == (3, Decimal(NETWORK_AMOUNT))


def test_the_period_is_the_one_the_fixture_uses(db):
    """Страховка от бессмысленно зелёных проверок выше.

    Все они смотрят в `pnl_lines` по `parent_fact_id`. Если бы разнесение
    легло в другой месяц или представление перестало отдавать детей, проверки
    остались бы зелёными на пустоте — здесь фиксируется, что материал есть.
    """
    fact_id = spread_expense(db)
    assert db.execute(
        "select distinct period from facts where parent_fact_id = %s", (fact_id,)
    ).fetchall() == [(_june(),)]


def _june():
    from datetime import date

    return date.fromisoformat(JUNE)
