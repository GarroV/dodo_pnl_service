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
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
    pay_component,
)
from domain_probe import fill_empty_tables

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


# Таблицы каркаса Django. Данных продукта в них нет, политик на них не заводим —
# всё остальное в схеме обязано быть закрыто.
#
# Таблицы очереди (`django_q_*`, T024) — тот же случай, но по другой причине, и
# её нужно понимать. Колонки тенанта в них нет и быть не может: очередь общая, а
# полезная нагрузка — подписанный `SECRET_KEY` пакет с именем функции и
# идентификаторами. Политике там нечего фильтровать. Поэтому они закрыты не
# политикой, а привилегиями: миграция `0047_queue_privileges` забирает у
# `app_user` всё, кроме права поставить задачу. Что закрытие настоящее,
# проверяет `test_queue_tables_are_closed_by_privileges` — без него это
# исключение молча стало бы дырой в проверке, которая ищет ровно такие дыры.
QUEUE_TABLES = {"django_q_ormq", "django_q_task", "django_q_schedule"}


class _Rollback(Exception):
    """Выйти из вложенной транзакции, не оставив за собой строки."""

FRAMEWORK_TABLES = {
    "django_migrations", "django_content_type", "django_session",
    "auth_permission", "auth_group", "auth_group_permissions",
    "auth_user", "auth_user_groups", "auth_user_user_permissions",
} | QUEUE_TABLES


def domain_tables(conn) -> list[str]:
    """Таблицы продукта — как они есть в базе, а не как их помнит тест.

    Список берётся из схемы специально: новая таблица, заведённая без политик,
    должна ронять проверку сама, без того чтобы кто-то вспомнил дописать её сюда.
    """
    rows = conn.execute(
        "select tablename from pg_tables where schemaname = 'public'"
    ).fetchall()
    tables = sorted({row[0] for row in rows} - FRAMEWORK_TABLES)
    assert len(tables) >= 20, f"таблиц подозрительно мало ({len(tables)}) — проверка фиктивна"
    return tables


def test_force_rls_on_every_domain_table(db):
    """Без force политики не действуют на владельца — а миграции идут владельцем.

    Проверяется вся схема, а не список из головы: незакрытая таблица — это
    утечка между партнёрами, и обнаружиться она должна здесь.
    """
    rows = db.execute(
        """select relname from pg_class
            where relname = any(%s) and not (relrowsecurity and relforcerowsecurity)""",
        (domain_tables(db),),
    ).fetchall()
    assert rows == [], f"RLS не принудительная на: {[r[0] for r in rows]}"


def test_schema_wide_checks_catch_an_unprotected_table(db):
    """Обе проверки выше обязаны падать на незакрытой таблице.

    Иначе они декоративные: список таблиц берётся из схемы, и без этого теста
    нельзя отличить «всё закрыто» от «ничего не нашли». Таблица создаётся внутри
    транзакции теста и исчезает вместе с ней.
    """
    db.execute("create table leaky_check (tenant_id uuid, note text)")
    db.execute("insert into leaky_check values (gen_random_uuid(), 'чужое')")
    db.execute("grant select on leaky_check to app_user")

    assert "leaky_check" in domain_tables(db)
    with pytest.raises(AssertionError):
        test_force_rls_on_every_domain_table(db)
    with pytest.raises(AssertionError):
        test_no_domain_table_returns_rows_without_context(db)


def test_queue_tables_are_closed_by_privileges(db):
    """Исключение для очереди держится на привилегиях — проверяем, что на деле.

    Таблицы `django_q_*` выведены из проверки политик выше, потому что тенанта в
    них нет и политике нечего фильтровать. Значит, закрывать их обязано что-то
    другое, и это «другое» должно проверяться, иначе исключение — просто дыра.
    """
    import psycopg

    # Запрос на таблицу очереди свой: у `django_q_ormq` роли оставлено право на
    # колонку `id` (иначе не работает `insert ... returning`), поэтому `count(*)`
    # там проходит законно, а закрыта именно полезная нагрузка. Проверять
    # «count(*) падает» было бы проверкой не того, что закрыто.
    forbidden = {
        "django_q_ormq": "select payload from django_q_ormq",
        "django_q_task": "select count(*) from django_q_task",
        "django_q_schedule": "select count(*) from django_q_schedule",
    }
    assert set(forbidden) == QUEUE_TABLES, "список таблиц очереди разъехался"

    for query in forbidden.values():
        with as_app_user(db, USER_DIRECTOR) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(query).fetchall()

    # А поставить задачу роль обязана уметь: иначе фоновый расчёт не запустился бы.
    with as_app_user(db, USER_DIRECTOR) as conn, pytest.raises(_Rollback):
        with conn.transaction():
            assert conn.execute(
                "insert into django_q_ormq (key, payload, lock) "
                "values ('t', 'x', now()) returning id"
            ).fetchone()[0]
            # Строку за собой не оставляем: исключение откатывает вложенную
            # транзакцию, а сам тест на этом заканчивается успехом.
            raise _Rollback()


def test_the_queue_worker_role_may_work_the_queue(db):
    """Роль очереди читает и правит очередь целиком — иначе она не работает.

    Дефект, ради которого роль появилась (issue #66): продукт и очередь ходили
    одной ролью, а `0047` оставляет `app_user` на очереди только право поставить
    задачу — рабочий процесс падал `permission denied for table django_q_ormq`.
    Разбирать очередь ролью приложения нельзя (полезная нагрузка ей не нужна),
    поэтому у очереди роль своя, и права ей выдаёт миграция, а не рука на
    площадке: сделанное руками до площадки не доедет.
    """
    for table in sorted(QUEUE_TABLES):
        granted = db.execute(
            "select has_table_privilege('queue_worker', %s, 'select, insert, update, delete')",
            (table,),
        ).fetchone()[0]
        assert granted, f"роль очереди не может работать с {table}"

    # Ключи задач берутся из последовательности: без права на неё `insert` не
    # проходит, и очередь встала бы уже на постановке задачи.
    assert db.execute(
        "select has_sequence_privilege('queue_worker', 'django_q_ormq_id_seq', 'usage')"
    ).fetchone()[0]


def test_the_queue_role_does_not_leak_into_the_product_role(db):
    """`app_user` не член роли очереди — иначе `0047` отменяется сама собой.

    Права членов складываются: сделай роль продукта членом `queue_worker`, и
    полезная нагрузка снова видна каждому запросу, при том что соседний тест
    по-прежнему зелёный — он спрашивает права `app_user`, а не её членства.
    Поэтому проверяется именно членство, и проверяется в обе стороны.
    """
    assert db.execute(
        "select pg_has_role('app_user', 'queue_worker', 'member')"
    ).fetchone()[0] is False, "роль продукта состоит в роли очереди — 0047 отменена"
    assert db.execute(
        "select pg_has_role('queue_worker', 'app_user', 'member')"
    ).fetchone()[0] is False, (
        "роль очереди состоит в роли продукта — доступ к данным она получает "
        "внутри задачи через set local role, а не постоянным членством"
    )


def test_no_domain_table_returns_rows_without_context(db):
    """Гарантия наружу: без контекста пусто везде, а не только там, где смотрели.

    Перед счётом в каждую пустую таблицу кладётся пробная строка, и это не
    придирка к чистоте. Пустая таблица отдаёт ноль строк при любой политике —
    и с настоящей, и с `using (true)`, и без политики вовсе, — то есть
    проверенной она только выглядит. Ровно так ослабленная политика на
    `payrun_jobs` не покраснела ни одним тестом (issue #63). Таблица, куда
    строку положить не удалось, считается **непроверенной** и валит прогон по
    имени: пропускать её молча — значит вернуть ту же слепоту списком
    исключений.
    """
    tables = domain_tables(db)
    failures = fill_empty_tables(db, tables)
    assert not failures, (
        "в эти таблицы не удалось положить пробную строку, поэтому политика на "
        "них НЕ проверена: "
        + "; ".join(f"{table} — {reason}" for table, reason in sorted(failures.items()))
        + ". Допишите значение по смыслу в MEANINGFUL_VALUES (tests/domain_probe.py) "
        "или заведите строку в _seed (tests/conftest.py)"
    )

    empty = [
        table for table in tables
        if db.execute(f"select count(*) from {table}").fetchone()[0] == 0
    ]
    assert empty == [], f"пустая таблица политику не проверяет: {empty}"

    with as_app_user(db, None) as conn:
        leaking = [
            table
            for table in tables
            if conn.execute(f"select count(*) from {table}").fetchone()[0] > 0
        ]
    assert leaking == [], f"без контекста видны строки: {leaking}"


def test_the_check_sees_an_empty_table_that_leaks(db):
    """Дыра, ради которой всё это писалось: таблица без политики и без строк.

    До починки (issue #63) такая таблица проезжала перебор молча: строк нет —
    считать нечего. Теперь проверка сама кладёт в неё строку и обязана упасть.
    Отличие от соседнего `test_schema_wide_checks_catch_an_unprotected_table` в
    одном слове: там таблица создаётся **со строкой**, и потому её ловила даже
    прежняя проверка. Здесь — пустая, и это тот самый случай, который проезжал.
    """
    db.execute("create table leaky_empty (tenant_id uuid, note text)")
    db.execute("grant select, insert on leaky_empty to app_user")
    assert "leaky_empty" in domain_tables(db)
    assert db.execute("select count(*) from leaky_empty").fetchone()[0] == 0

    with pytest.raises(AssertionError) as refusal:
        test_no_domain_table_returns_rows_without_context(db)
    # Причина падения проверяется дословно: «упало» тут можно получить тремя
    # разными способами, и два из них означали бы, что дыру мы так и не видим —
    # проверка споткнулась о собственный наполнитель.
    assert "без контекста видны строки" in str(refusal.value), str(refusal.value)
    assert "leaky_empty" in str(refusal.value)


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
    """Роль с неполным набором регистров видит только свои.

    Роль здесь — управляющий точки (официальный и дополнительный, D031). До
    D036 на этом месте стоял бухгалтер, но её набор теперь полон, и отказ ей
    не отличить от снятой политики. Проверка обязана стоять на роли, у которой
    набор действительно неполный, — иначе механизм видимости остался бы без
    единой проверки.

    Дефект, ради которого написан тест: пермиссивные политики Postgres
    объединяет через OR, поэтому политика видимости регистра не сужала выборку
    вообще — строку своего тенанта пропускала политика изоляции. Сужает только
    `as restrictive`.
    """
    pay_component(db, ledger="official", amount="100.00", code="official.one")
    pay_component(db, ledger="supplementary", amount="30.00", code="supplementary.one")
    pay_component(db, ledger="internal", amount="200.00", code="internal.one")

    with as_app_user(db, USER_MANAGER) as conn:
        ledgers = {row[0] for row in conn.execute("select ledger from pay_components").fetchall()}
    assert ledgers == {"official", "supplementary"}


def test_ledger_visibility_affects_totals(db):
    """Невидимый регистр не должен просачиваться и в итоги."""
    pay_component(db, ledger="official", amount="100.00", code="official.one")
    pay_component(db, ledger="supplementary", amount="30.00", code="supplementary.one")
    pay_component(db, ledger="internal", amount="200.00", code="internal.one")

    with as_app_user(db, USER_MANAGER) as conn:
        total = conn.execute("select coalesce(sum(amount), 0) from pay_components").fetchone()[0]
    assert total == 130  # официальный и дополнительный, без внутреннего

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

    with as_app_user(db, USER_MANAGER) as conn:
        ledgers = {row[0] for row in conn.execute("select ledger from allocation_rules").fetchall()}
    assert ledgers == {"official"}
