"""Разграничение доступа к фактам: тенант, точки роли, регистры учёта (T107).

Все проверки гоняются **ролью `app_user`**. Владелец таблиц обходит `force row
level security`, а суперпользователь обходит её всегда, поэтому тот же набор
проверок, выполненный владельцем, был бы зелёным при снятых политиках — то есть
бесполезным. На этом проекте так уже прожил незамеченным дефект видимости
регистров, и повторять его здесь нечем.

Каждая проверка написана так, чтобы **краснеть от порчи своей политики**:
`using (true)` вместо условия обязан ронять ровно её. Проверено вручную по
очереди на каждой из трёх политик — журнал блока `docs/forge/blocks/facts.md`.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from conftest import (
    CP_EPS,
    I_FOOD,
    I_REVENUE,
    JUNE,
    T1,
    T2,
    U_BG1,
    U_NS1,
    U_NS2,
    U_OTHER,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
)
from facts_helpers import fact_payload, upsert_fact

# --- изоляция тенантов -------------------------------------------------------

def test_other_tenant_facts_are_invisible(db):
    """Факт чужого партнёра не виден ни строкой, ни в сумме представления."""
    upsert_fact(db, fact_payload(tenant=T1, unit=U_BG1, amount="100.00", key="own"))
    upsert_fact(db, fact_payload(tenant=T2, unit=U_OTHER, amount="777.00", key="alien"))

    with as_app_user(db, USER_DIRECTOR) as conn:
        keys = {row[0] for row in conn.execute("select dedup_key from facts").fetchall()}
        total = conn.execute("select coalesce(sum(amount), 0) from pnl_lines").fetchone()[0]
    assert keys == {"own"}
    assert total == 100


def test_tenant_check_is_meaningful(db):
    """Тот же запрос владельцем видит оба тенанта — проверка выше ловит именно RLS."""
    upsert_fact(db, fact_payload(tenant=T1, unit=U_BG1, amount="100.00", key="own"))
    upsert_fact(db, fact_payload(tenant=T2, unit=U_OTHER, amount="777.00", key="alien"))

    keys = {row[0] for row in db.execute("select dedup_key from facts").fetchall()}
    assert keys == {"own", "alien"}


def test_write_into_other_tenant_is_rejected(db):
    """Забытый фильтр на записи тоже не проходит: `with check` закрывает вставку."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            upsert_fact(conn, fact_payload(tenant=T2, unit=U_OTHER, key="hack"))


def test_documents_and_batches_are_isolated(db):
    """Документ и партия загрузки закрыты тенантом так же, как сами факты."""
    for tenant, external in ((T1, "doc-own"), (T2, "doc-alien")):
        db.execute(
            """insert into source_documents (tenant_id, kind, source, external_id, doc_date)
               values (%s, 'invoice', 'manual', %s, %s)""",
            (tenant, external, JUNE),
        )
        db.execute(
            "insert into fact_batches (tenant_id, source, external_ref) values (%s, 'manual', %s)",
            (tenant, external),
        )

    with as_app_user(db, USER_DIRECTOR) as conn:
        docs = {
            row[0]
            for row in conn.execute("select external_id from source_documents").fetchall()
        }
        batches = {
            row[0] for row in conn.execute("select external_ref from fact_batches").fetchall()
        }
    assert docs == {"doc-own"}
    assert batches == {"doc-own"}


def test_other_tenant_user_sees_only_his_own(db):
    upsert_fact(db, fact_payload(tenant=T1, unit=U_BG1, key="own"))
    upsert_fact(db, fact_payload(tenant=T2, unit=U_OTHER, key="alien"))

    with as_app_user(db, USER_OTHER) as conn:
        keys = {row[0] for row in conn.execute("select dedup_key from facts").fetchall()}
    assert keys == {"alien"}


# --- точки роли --------------------------------------------------------------

def test_manager_sees_only_his_units(db):
    """Управляющий видит факты своей точки; чужие не видит ни строкой, ни в сумме."""
    upsert_fact(db, fact_payload(unit=U_NS1, amount="10.00", key="mine"))
    upsert_fact(db, fact_payload(unit=U_BG1, amount="20.00", key="not-mine"))
    upsert_fact(db, fact_payload(unit=U_NS2, amount="40.00", key="not-mine-either"))

    with as_app_user(db, USER_MANAGER) as conn:
        keys = {row[0] for row in conn.execute("select dedup_key from facts").fetchall()}
        total = conn.execute("select coalesce(sum(amount), 0) from pnl_by_unit").fetchone()[0]
    assert keys == {"mine"}
    assert total == 10


def test_manager_cannot_write_into_another_unit(db):
    """Иначе чужую точку было бы не видно, но можно было бы в неё вписать."""
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            upsert_fact(conn, fact_payload(unit=U_BG1, key="into-alien-unit"))


def test_accountant_sees_every_unit(db):
    """Ограничение по точкам не должно задевать роль, которой точки не сужали."""
    upsert_fact(db, fact_payload(unit=U_NS1, amount="10.00", key="ns1"))
    upsert_fact(db, fact_payload(unit=U_BG1, amount="20.00", key="bg1"))

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        keys = {row[0] for row in conn.execute("select dedup_key from facts").fetchall()}
    assert keys == {"ns1", "bg1"}


def test_a_network_fact_is_visible_only_to_whoever_runs_every_unit(db):
    """Факт без точки — трата юрлица целиком, а не ничья строка (T130).

    Так было записано наоборот: «строка без точки видна всем в тенанте»
    (`app_unit_is_visible`, миграция `0011`). Правило писалось тогда, когда
    фактов без точки в продукте не существовало и null означал «точку удалили».
    С появлением расхода на всю сеть (T111) тот же null стал значить «аренда
    офиса, реклама на сеть» — и старое правило открыло управляющему точки
    расходы юрлица: на демо в его списке стояли `Marketing campaign 150 000` и
    `Office rent 90 000`, а итог «для сверки с кассой» был завышен в семнадцать
    раз (сверка 7, находка 1).

    Спека прямо обратного мнения: управляющий «видит СВОИ расходы за месяц».
    """
    upsert_fact(
        db,
        fact_payload(unit=None, allocation="pending", counterparty=CP_EPS,
                     amount="500.00", key="network"),
    )

    with as_app_user(db, USER_MANAGER) as conn:
        assert conn.execute("select count(*) from facts").fetchone()[0] == 0
        assert conn.execute("select count(*) from facts_unallocated").fetchone()[0] == 0
        # И следа в суммах тоже нет: спрятать строку, оставив её слагаемым, —
        # тот же показ чужих денег, только одним числом (D023).
        assert conn.execute(
            "select coalesce(sum(amount), 0) from pnl_lines"
        ).fetchone()[0] == 0

    # Контроль: тот, кто ведёт партнёра целиком, видит его как раньше — иначе
    # проверка выше была бы зелёной и на потерянной строке (D036).
    for user in (USER_DIRECTOR, USER_ACCOUNTANT):
        with as_app_user(db, user) as conn:
            assert conn.execute(
                "select count(*) from facts_unallocated"
            ).fetchone()[0] == 1, f"нераспределённый факт не виден пользователю {user}"


def test_a_role_with_limited_units_cannot_write_a_network_fact(db):
    """Внести расход на всю сеть — то же самое, что записать на чужие точки.

    Иначе управляющий кладёт сумму, которую любой директор потом разнесёт по
    трём точкам: на демо так прошли 500 000 (сверка 7, находка 1).
    """
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            upsert_fact(conn, fact_payload(unit=None, allocation="pending",
                                           key="network-by-manager"))


def test_a_role_with_limited_units_cannot_change_or_remove_a_network_fact(db):
    """Не только просмотр: правка и удаление сетевой строки тоже не его.

    Проверяется числом изменённых строк, а не отсутствием ошибки: политика не
    отказывает на `update`, она просто не показывает строку — и правка молча
    проходит мимо цели. Именно так и выглядел дефект: POST на карточку чужого
    сетевого расхода отвечал 302, а сумма 999,99 становилась 1,00.
    """
    fact_id, _ = upsert_fact(
        db, fact_payload(unit=None, allocation="pending", amount="999.99",
                         key="network-money"),
    )

    with as_app_user(db, USER_MANAGER) as conn:
        changed = conn.execute(
            "update facts set amount = 1.00 where id = %s", (fact_id,)
        ).rowcount
        removed = conn.execute(
            "update facts set superseded_at = now() where id = %s", (fact_id,)
        ).rowcount
    assert (changed, removed) == (0, 0)

    assert db.execute(
        "select amount, superseded_at from facts where id = %s", (fact_id,)
    ).fetchone() == (Decimal("999.99"), None)


# --- регистры учёта ----------------------------------------------------------
# Роль с неполным набором регистров в фикстуре ровно одна — управляющий точки
# (официальный и дополнительный, без внутреннего; D031). После D036 набор
# бухгалтера и директора полон, и отказ им нельзя было бы отличить от снятой
# политики.

def _three_ledgers(conn) -> None:
    for ledger, amount in (("official", "100.00"), ("supplementary", "30.00"),
                           ("internal", "200.00")):
        upsert_fact(
            conn,
            fact_payload(unit=U_NS1, ledger=ledger, amount=amount, key=f"ledger-{ledger}"),
        )


def test_ledger_visibility_narrows_not_widens(db):
    """Строк невидимого регистра нет вовсе.

    Пермиссивные политики Postgres объединяет через OR, поэтому «регистр видим»
    не сужало бы выборку вообще: строку своего тенанта пропустила бы политика
    изоляции. Сужает только `as restrictive`.
    """
    _three_ledgers(db)

    with as_app_user(db, USER_MANAGER) as conn:
        ledgers = {row[0] for row in conn.execute("select ledger from facts").fetchall()}
    assert ledgers == {"official", "supplementary"}


def test_ledger_visibility_affects_sums(db):
    """Ни строк, ни следа в итогах (D023): суммы считаются по видимому срезу."""
    _three_ledgers(db)

    with as_app_user(db, USER_MANAGER) as conn:
        total = conn.execute("select coalesce(sum(amount), 0) from pnl_by_unit").fetchone()[0]
        network = conn.execute(
            "select coalesce(sum(amount), 0) from pnl_by_network"
        ).fetchone()[0]
    assert total == 130    # официальный и дополнительный, без внутреннего
    assert network == 130

    with as_app_user(db, USER_DIRECTOR) as conn:
        total = conn.execute("select coalesce(sum(amount), 0) from pnl_by_unit").fetchone()[0]
    assert total == 330


def test_writing_into_an_invisible_ledger_is_rejected(db):
    """Иначе ограничение обходится вставкой: не вижу, но записать могу."""
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            upsert_fact(conn, fact_payload(unit=U_NS1, ledger="internal", key="hidden"))


def test_report_function_respects_the_ledger_slice(db):
    """Готовый отчёт собирается из тех же строк, а не мимо политик."""
    _three_ledgers(db)

    with as_app_user(db, USER_MANAGER) as conn:
        rows = conn.execute(
            "select code, amount from pnl_report(%s, %s) where code = 'food_cost'",
            (T1, JUNE),
        ).fetchall()
    assert rows == [("food_cost", 130)]


# --- представления ходят глазами смотрящего ----------------------------------

def test_views_are_security_invoker(db):
    """Без `security_invoker` представление читает данные правами владельца.

    Тогда RLS перестаёт работать вообще: и регистры, и тенанты потекут через
    любой отчёт. Проверяется свойство, а не только следствие — следствие ловится
    выше, а здесь видно причину.
    """
    rows = db.execute(
        """select c.relname, c.reloptions
             from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relkind = 'v'
              and c.relname in ('pnl_lines', 'pnl_by_unit', 'pnl_by_network',
                                'facts_unallocated')"""
    ).fetchall()
    assert len(rows) == 4, f"нашлись не все представления: {rows}"
    for name, options in rows:
        assert options and "security_invoker=true" in options, f"{name}: {options}"


def test_no_facts_without_context(db):
    """Контекст не выставлен — пусто, а не «всё подряд»."""
    upsert_fact(db, fact_payload(unit=U_BG1, key="whatever"))

    with as_app_user(db, None) as conn:
        for relation in ("facts", "source_documents", "fact_batches", "pnl_lines",
                         "pnl_by_unit", "pnl_by_network", "facts_unallocated"):
            count = conn.execute(f"select count(*) from {relation}").fetchone()[0]
            assert count == 0, f"без контекста видны строки в {relation}"


def test_app_user_can_read_the_views(db):
    """Привилегии на новые таблицы и представления роль обязана получить.

    Иначе разграничение выглядело бы работающим, а продукт не читал бы ничего:
    отказ по привилегии и отказ по политике — разные вещи, и путать их нельзя.
    """
    upsert_fact(db, fact_payload(unit=U_BG1, item=I_REVENUE, amount="1.00", key="readable"))

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select count(*) from pnl_lines").fetchone()[0] == 1
        assert conn.execute("select count(*) from pnl_by_unit").fetchone()[0] == 1


def test_food_item_is_expense(db):
    """Страховка от переписанного сида: суммы выше сняты на статье-расходе."""
    kind = db.execute("select kind from pnl_items where id = %s", (I_FOOD,)).fetchone()[0]
    assert kind == "expense"
