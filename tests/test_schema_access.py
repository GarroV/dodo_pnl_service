"""
Разграничение доступа: изоляция тенантов и видимость регистров учёта.

Главное правило этих тестов: они гоняются ролью `app_user`. Владелец таблиц
и суперпользователь обходят RLS, поэтому тот же набор проверок, выполненный
владельцем, обязан быть зелёным при любых политиках — то есть бесполезным.
Проверка `test_isolation_check_is_meaningful` фиксирует это явно: если она
перестанет падать «наоборот», значит остальные тесты доказывают не то, что
думают.
"""
from __future__ import annotations

import pytest

from conftest import (
    T1,
    T2,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_OTHER,
    as_app_user,
    pay_component,
)

TENANT_TABLES = [
    "legal_entities", "units", "roles", "memberships", "pnl_items",
    "counterparties", "allocation_rules", "periods", "rule_overrides",
    "employee_groups", "employees", "employment_terms", "timesheets",
    "payruns", "payslips", "pay_components",
]


def _unit_tenants(conn) -> set[str]:
    return {str(row[0]) for row in conn.execute("select tenant_id from units").fetchall()}


# --- Роль приложения ---------------------------------------------------------

def test_app_user_cannot_bypass_rls(db):
    """Роль приложения не суперпользователь и не имеет bypassrls."""
    row = db.execute(
        "select rolsuper, rolbypassrls, rolcanlogin from pg_roles where rolname = 'app_user'"
    ).fetchone()
    assert row is not None, "роль app_user не создана миграцией"
    is_super, bypass, _ = row
    assert is_super is False
    assert bypass is False


def test_app_user_does_not_own_tables(db):
    """Владелец таблиц обходит даже force RLS — значит владеть должен не app_user."""
    owned = db.execute(
        "select tablename from pg_tables where schemaname = 'public' and tableowner = 'app_user'"
    ).fetchall()
    assert owned == []


def test_force_rls_on_every_tenant_table(db):
    """Без force политики не действуют на владельца — а миграции идут владельцем."""
    rows = db.execute(
        """select relname from pg_class
            where relname = any(%s) and not (relrowsecurity and relforcerowsecurity)""",
        (TENANT_TABLES,),
    ).fetchall()
    assert rows == [], f"RLS не принудительная на: {[r[0] for r in rows]}"


# --- Доменные типы -----------------------------------------------------------

def test_enum_array_reads_as_list(db):
    """Массив регистров учёта должен приезжать списком, а не строкой.

    Без регистрации типа в драйвере `ledger[]` приходит одной строкой
    `'{official,supplementary,internal}'`, и проверка «регистр входит в видимые» начинает
    работать по символам, ничего не сообщая. Что проверка не фиктивная —
    показано в test_seed_dev.test_enum_array_without_registration_is_a_string.
    """
    row = db.execute("select visible_ledgers from roles where code = 'director'").fetchone()
    assert isinstance(row[0], list)
    assert set(row[0]) == {"official", "supplementary", "internal"}


# --- Изоляция тенантов -------------------------------------------------------

def test_tenant_isolation_hides_other_tenant(db):
    """Пользователь тенанта A не получает ни одной строки тенанта B."""
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        assert _unit_tenants(conn) == {T1}


def test_isolation_check_is_meaningful(db):
    """Тот же запрос владельцем видит оба тенанта — проверка ловит именно RLS.

    Если однажды здесь окажется один тенант, значит тест изоляции выше
    доказывает не работу политик, а что-то другое.
    """
    assert _unit_tenants(db) == {T1, T2}


def test_other_tenant_user_sees_only_his_own(db):
    with as_app_user(db, USER_OTHER) as conn:
        assert _unit_tenants(conn) == {T2}


def test_no_context_no_rows(db):
    """Контекст не выставлен — выборка пуста, а не «всё подряд»."""
    with as_app_user(db, None) as conn:
        assert conn.execute("select count(*) from units").fetchone()[0] == 0
        assert conn.execute("select count(*) from employees").fetchone()[0] == 0


def test_write_into_other_tenant_is_rejected(db):
    """Забытый фильтр на записи тоже не проходит: with check закрывает вставку."""
    import psycopg

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        # transaction() внутри открытой транзакции = точка сохранения:
        # после отказа соединение остаётся рабочим и as_app_user доберётся
        # до своего reset role.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "insert into units (tenant_id, code, title) values (%s, 'HACK', 'Чужая')",
                (T2,),
            )


# --- Общие данные без тенанта (T002) -----------------------------------------
# Часть справочников по замыслу не принадлежит никому: системные роли и общий
# справочник статей P&L (`tenant_id is null`). Единый справочник — цель проекта,
# поэтому «не видно никому» здесь не мелочь: на нём собирается P&L всей сети.


def test_shared_pnl_items_are_visible(db):
    """Общий справочник статей виден пользователю любого тенанта.

    Дефект, ради которого написан тест: `null in (select ...)` даёт null, то есть
    «не проходит», и строки без тенанта не видел никто.
    """
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        codes = {row[0] for row in conn.execute("select code from pnl_items").fetchall()}
    assert "revenue" in codes and "labour_cost" in codes


def test_system_roles_are_visible(db):
    """Системная роль (без тенанта) видна — иначе экран управления ролями пуст."""
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        codes = {row[0] for row in conn.execute("select code from roles").fetchall()}
    assert "support" in codes, "системная роль не видна"
    assert "director" in codes, "роль своего тенанта пропала вместе с починкой"


def test_shared_rows_stay_hidden_without_context(db):
    """Общее — не значит публичное: без контекста по-прежнему пусто."""
    with as_app_user(db, None) as conn:
        assert conn.execute("select count(*) from pnl_items").fetchone()[0] == 0
        assert conn.execute("select count(*) from roles").fetchone()[0] == 0


def test_other_tenant_rows_stay_hidden_after_the_fix(db):
    """Починка общего не должна открыть чужое: у второго тенанта своя роль."""
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        titles = {row[0] for row in conn.execute("select title from roles").fetchall()}
    assert "Директор партнёра" not in titles


def test_app_user_cannot_create_shared_rows(db):
    """Записывать общий справочник приложению незачем: только читать.

    Иначе любой пользователь любого партнёра правил бы справочник всей сети.
    """
    import psycopg

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "insert into pnl_items (tenant_id, code, title, kind)"
                " values (null, 'hack', 'Своя статья', 'expense')"
            )


# --- Видимость регистров учёта ----------------------------------------------

def test_ledger_visibility_narrows_not_widens(db):
    """Бухгалтер видит только официальный регистр.

    Дефект, ради которого написан тест: пермиссивные политики Postgres
    объединяет через OR, поэтому политика видимости регистра не сужала выборку
    вообще — строку своего тенанта пропускала политика изоляции. Сужает только
    `as restrictive`.
    """
    pay_component(db, ledger="official", amount="100.00", code="official.one")
    pay_component(db, ledger="internal", amount="200.00", code="internal.one")

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        ledgers = {row[0] for row in conn.execute("select ledger from pay_components").fetchall()}
    assert ledgers == {"official"}


def test_ledger_visibility_affects_totals(db):
    """Невидимый регистр не должен просачиваться и в итоги."""
    pay_component(db, ledger="official", amount="100.00", code="official.one")
    pay_component(db, ledger="supplementary", amount="30.00", code="supplementary.one")
    pay_component(db, ledger="internal", amount="200.00", code="internal.one")

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        total = conn.execute("select coalesce(sum(amount), 0) from pay_components").fetchone()[0]
    assert total == 100

    with as_app_user(db, USER_DIRECTOR) as conn:
        total = conn.execute("select coalesce(sum(amount), 0) from pay_components").fetchone()[0]
    assert total == 330


def test_ledger_visibility_applies_to_allocation_rules(db):
    """Вторая таблица с регистром — правила разнесения — закрыта так же."""
    cp = db.execute("select id from counterparties limit 1").fetchone()[0]
    item = db.execute("select id from pnl_items limit 1").fetchone()[0]
    for ledger in ("official", "internal"):
        db.execute(
            """insert into allocation_rules
                   (tenant_id, counterparty_id, pnl_item_id, method, ledger, valid_from)
               values (%s, %s, %s, 'even', %s, '2026-01-01')""",
            (T1, cp, item, ledger),
        )

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        ledgers = {row[0] for row in conn.execute("select ledger from allocation_rules").fetchall()}
    assert ledgers == {"official"}
