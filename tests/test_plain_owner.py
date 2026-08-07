"""
Схема без суперпользователя: развёртывание обычной ролью (T052, issue #44).

Зачем этот модуль отдельно от `test_schema_access.py`. Все остальные тесты
схемы работают на базе, которую накатил суперпользователь — в образе
`pgvector/pgvector:pg17` роль из `POSTGRES_USER` именно такая, и локально тоже.
Суперпользователь обходит RLS всегда, включая `force row level security`.
Поэтому конструкция «политика зовёт функцию, а функция читает таблицу с той же
политикой» на такой базе выглядит рабочей, хотя держится она не на замысле, а
на привилегии владельца.

На управляемом Postgres суперпользователя не дают, и `bypassrls` выдать тоже
некому: этот атрибут меняет только суперпользователь. Значит требование
«владелец обходит RLS» — это не настройка площадки, а «продукт туда не ставится».

Здесь всё проверяется на роли, у которой нет ни `superuser`, ни `bypassrls`:
она накатывает миграции, а приложение ходит отдельной логин-ролью, тоже без
привилегий. Если однажды в схеме появится политика, которой снова нужен обход,
красным станет этот модуль, а не площадка заказчика.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from conftest import (
    ADMIN_DSN,
    T1,
    T2,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    _seed,
    run_manage,
)

# Таблицы продукта: те же, что в контракте блока. Здесь список нужен для
# проверки «без контекста пусто», поэтому берётся из схемы, а не из головы.
PRODUCT_TABLES_SQL = """
select c.relname
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity
 order by c.relname
"""

OWNER_PASSWORD = "plain-owner-test-only"
APP_PASSWORD = "app-login-test-only"


def _migrate(dsn: str) -> None:
    """Накатить схему, показав причину, если не вышло.

    Голый `CalledProcessError` прячет текст ошибки Postgres в stderr
    подпроцесса — а весь смысл этого модуля именно в том, чтобы причина отказа
    читалась сразу.
    """
    import subprocess

    try:
        run_manage(dsn, "migrate", "--no-input")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"миграции не накатились ролью без bypassrls:\n{exc.stderr}")


@contextmanager
def _plain_owner_cluster():
    """База, накатанная ролью без права обходить RLS, плюс логин-роль приложения.

    Роли и база временные, с pid в имени: параллельный прогон в том же кластере
    не должен натыкаться на чужие.
    """
    psycopg = pytest.importorskip("psycopg", reason="нужен psycopg: pip install -e '.[dev]'")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"нет доступного Postgres по {ADMIN_DSN}: {exc}")

    pid = os.getpid()
    owner = f"dodo_owner_{pid}"
    app_login = f"dodo_app_{pid}"
    dbname = f"dodo_pnl_test_owner_{pid}"

    params = conninfo_to_dict(ADMIN_DSN)
    host = params.get("host") or "localhost"
    port = params.get("port") or "5432"

    def owner_dsn(db: str) -> str:
        return make_conninfo(
            "", host=host, port=port, dbname=db, user=owner, password=OWNER_PASSWORD
        )

    def app_dsn(db: str) -> str:
        return make_conninfo(
            "", host=host, port=port, dbname=db, user=app_login, password=APP_PASSWORD
        )

    with admin:
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        for role in (owner, app_login):
            admin.execute(f'drop role if exists "{role}"')
        # createrole нужен, чтобы миграция могла завести роль приложения; права
        # обходить RLS роль не получает намеренно — в этом весь смысл проверки.
        admin.execute(
            f"""create role "{owner}" login password '{OWNER_PASSWORD}'
                nosuperuser nobypassrls createrole"""
        )
        admin.execute(
            f"""create role "{app_login}" login password '{APP_PASSWORD}'
                nosuperuser nobypassrls"""
        )
        admin.execute(f'create database "{dbname}" owner "{owner}"')
        # На чистой площадке роли `app_user` ещё нет и миграция заводит её сама
        # (создатель роли получает права её администрировать). Здесь кластер
        # общий и роль в нём давно есть — поэтому воспроизводится второй
        # штатный путь: роль завёл администратор и выдал её тому, кто
        # накатывает миграции. Ветку «создаём сами» на общем кластере проверить
        # нельзя, не сломав соседние базы.
        admin.execute(f'grant app_user to "{owner}" with admin option')

    try:
        _migrate(owner_dsn(dbname))
        # Данные заводит администратор — это ровно та операция, которая обход
        # политик подразумевает (сид, восстановление дампа, обслуживание).
        with psycopg.connect(make_conninfo(ADMIN_DSN, dbname=dbname)) as seeder:
            _seed(seeder)
            seeder.commit()
        with psycopg.connect(make_conninfo(ADMIN_DSN, dbname=dbname), autocommit=True) as grants:
            grants.execute(f'grant connect on database "{dbname}" to "{app_login}"')
            grants.execute(f'grant app_user to "{app_login}"')
        yield {"dbname": dbname, "owner_dsn": owner_dsn(dbname), "app_dsn": app_dsn(dbname)}
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as cleanup:
            cleanup.execute(f'drop database if exists "{dbname}" with (force)')
            for role in (owner, app_login):
                cleanup.execute(f'drop owned by "{role}"')
                cleanup.execute(f'drop role if exists "{role}"')


@pytest.fixture(scope="module")
def plain_owner():
    with _plain_owner_cluster() as env:
        yield env


@pytest.fixture
def app_conn(plain_owner):
    """Соединение логин-ролью приложения. Всё написанное откатывается."""
    import psycopg

    from core.db_types import register_enum_types

    conn = psycopg.connect(plain_owner["app_dsn"])
    register_enum_types(conn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture(scope="module")
def two_ledgers(plain_owner):
    """Две выплаты разных регистров. Заводит администратор — как и весь сид."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    from conftest import pay_component

    with psycopg.connect(make_conninfo(ADMIN_DSN, dbname=plain_owner["dbname"])) as conn:
        pay_component(conn, ledger="official", amount="100.00", code="hours.regular")
        pay_component(conn, ledger="internal", amount="900.00", code="cash.extra")
        conn.commit()


@contextmanager
def as_app_user(conn, user_id: str | None):
    conn.execute("set local role app_user")
    conn.execute("select set_config('app.user_id', %s, true)", (user_id or "",))
    try:
        yield conn
    finally:
        conn.rollback()


# --- само развёртывание ------------------------------------------------------

def test_migrations_run_as_a_plain_owner(plain_owner):
    """Схема накатывается ролью без superuser и без bypassrls.

    Если это перестанет работать, продукт не ставится ни на один управляемый
    Postgres — а узнаётся это обычно на площадке заказчика.
    """
    import psycopg

    with psycopg.connect(plain_owner["owner_dsn"]) as conn:
        is_super, bypass = conn.execute(
            "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        ).fetchone()
        assert is_super is False and bypass is False
        applied = conn.execute(
            "select count(*) from django_migrations where app = 'core'"
        ).fetchone()[0]
        assert applied >= 10, f"миграции ядра не накатились: {applied}"


def test_the_login_role_of_the_application_cannot_bypass_rls(plain_owner):
    import psycopg

    with psycopg.connect(plain_owner["app_dsn"]) as conn:
        is_super, bypass = conn.execute(
            "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        ).fetchone()
        assert is_super is False and bypass is False
        owns = conn.execute(
            """select count(*) from pg_tables
                where schemaname = 'public' and tableowner = current_user"""
        ).fetchone()[0]
        assert owns == 0, "роль приложения не должна владеть таблицами"


# --- функции контекста без обхода RLS ---------------------------------------

def test_context_functions_work_without_a_bypass(app_conn):
    """Главная проверка задачи: политика зовёт функцию, и это не рекурсия.

    До починки здесь `stack depth limit exceeded`: политика на memberships
    звала app_tenant_ids(), а той нужна была та же политика.
    """
    with as_app_user(app_conn, USER_DIRECTOR) as conn:
        tenants = {str(row[0]) for row in conn.execute("select app_tenant_ids()").fetchall()}
        assert tenants == {T1}

        ledgers = conn.execute("select app_visible_ledgers(%s)", (T1,)).fetchone()[0]
        assert set(ledgers) == {"official", "supplementary", "internal"}


def test_isolation_holds_without_a_bypass(app_conn, plain_owner):
    import psycopg
    from psycopg.conninfo import make_conninfo

    # Сначала убеждаемся, что скрывать вообще есть что: без строк второго
    # тенанта в базе проверка изоляции была бы зелёной сама по себе.
    with psycopg.connect(make_conninfo(ADMIN_DSN, dbname=plain_owner["dbname"])) as admin:
        all_tenants = {
            str(row[0]) for row in admin.execute("select tenant_id from units").fetchall()
        }
    assert {T1, T2} <= all_tenants, all_tenants

    with as_app_user(app_conn, USER_DIRECTOR) as conn:
        tenants = {
            str(row[0]) for row in conn.execute("select tenant_id from units").fetchall()
        }
    assert tenants == {T1}, f"видны чужие тенанты: {tenants}"


def test_ledger_visibility_holds_without_a_bypass(app_conn, two_ledgers):
    """Бухгалтеру не видно строк скрытых от неё регистров."""
    with as_app_user(app_conn, USER_ACCOUNTANT) as conn:
        seen = {row[0] for row in conn.execute("select ledger from pay_components").fetchall()}
    assert seen == {"official"}, seen


def test_the_application_can_still_write(app_conn):
    """Не только чтение: запись под контекстом тоже не должна требовать обхода.

    Вставка тянет за собой `with check` четырёх таблиц и триггер, который
    дописывает набор регистров в строку ведомости, — то есть весь путь, каким
    расчёт кладёт результат.
    """
    from conftest import pay_component

    with as_app_user(app_conn, USER_DIRECTOR) as conn:
        component_id = pay_component(conn, ledger="supplementary", amount="1.00", code="probe")
        payslip = conn.execute(
            "select payslip_id from pay_components where id = %s", (component_id,)
        ).fetchone()[0]
        conn.execute(
            "insert into payslip_totals (tenant_id, payslip_id, net) values (%s, %s, 1.00)",
            (T1, payslip),
        )

        # Сработал ли триггер, спрашиваем не колонкой, а последствием: колонка
        # `payslips.ledgers` от роли приложения закрыта нарочно (T065). Зато на
        # ней стоит видимость итогов, и пустой набор прошёл бы у **любой** роли
        # (`'{}' <@ что угодно`) — то есть молча не сработавший триггер виден
        # здесь как утечка, а не как «ну и ладно».
        conn.execute("select set_config('app.user_id', %s, true)", (USER_ACCOUNTANT,))
        visible = conn.execute(
            "select count(*) from payslip_totals where payslip_id = %s", (payslip,)
        ).fetchone()[0]
    assert visible == 0, (
        "бухгалтеру видны итоги строки дополнительного регистра — значит набор "
        "регистров строки не заполнился"
    )


def test_nothing_is_visible_without_context(app_conn):
    """Без выставленного пользователя — ноль строк во всех закрытых таблицах."""
    tables = [row[0] for row in app_conn.execute(PRODUCT_TABLES_SQL).fetchall()]
    assert tables, "в схеме не нашлось ни одной таблицы с включённой RLS"

    leaking = {}
    with as_app_user(app_conn, None) as conn:
        for table in tables:
            count = conn.execute(f"select count(*) from {table}").fetchone()[0]
            if count:
                leaking[table] = count
    assert leaking == {}, f"без контекста видны строки: {leaking}"


def test_the_recursion_would_come_back_with_the_old_policy(plain_owner, app_conn):
    """Проверка на осмысленность: с прежней политикой всё это снова падает.

    Тест, зелёный и до, и после починки, ничего не доказывает. Здесь политика
    `memberships` на время возвращается к прежнему виду — «тенант из
    app_tenant_ids()» — и запрос обязан упасть рекурсией. Политика меняется
    отдельным соединением и возвращается в `finally`; база модульная и
    временная, поэтому даже сорванный прогон никому не мешает.
    """
    import psycopg
    from psycopg.conninfo import make_conninfo

    admin_dsn = make_conninfo(ADMIN_DSN, dbname=plain_owner["dbname"])
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("drop policy tenant_isolation on memberships")
        admin.execute("""
            create policy tenant_isolation on memberships
                for all
                using (tenant_id in (select app_tenant_ids()))
                with check (tenant_id in (select app_tenant_ids()))
        """)
        try:
            with pytest.raises(psycopg.errors.StatementTooComplex):
                with as_app_user(app_conn, USER_DIRECTOR) as conn:
                    conn.execute("select tenant_id from units").fetchall()
        finally:
            app_conn.rollback()
            admin.execute("drop policy tenant_isolation on memberships")
            admin.execute("""
                create policy tenant_isolation on memberships
                    for all
                    using (user_id = app_user_id())
                    with check (user_id = app_user_id())
            """)


def test_a_connection_that_forgets_the_role_sees_nothing(app_conn):
    """Запрос мимо помощника контекста.

    Найдено блоком auth: код, забывший выставить контекст, идёт ролью
    подключения. Пока ею был владелец-суперпользователь, такой запрос видел всё.
    С обычной логин-ролью он не видит ничего — то есть забывчивость даёт пустоту,
    а не утечку.
    """
    tables = [row[0] for row in app_conn.execute(PRODUCT_TABLES_SQL).fetchall()]
    leaking = {}
    for table in tables:
        count = app_conn.execute(f"select count(*) from {table}").fetchone()[0]
        if count:
            leaking[table] = count
    assert leaking == {}, f"роль подключения видит строки без контекста: {leaking}"
