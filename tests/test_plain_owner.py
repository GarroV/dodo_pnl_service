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
    USER_DIRECTOR,
    USER_MANAGER,
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
QUEUE_PASSWORD = "queue-login-test-only"


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
    # Третья логин-роль — рабочего процесса очереди (T124, issue #66). Она есть
    # именно здесь, а не в общей фикстуре: разделение ролей — свойство площадки,
    # и проверять его надо на конфигурации площадки, где ни у кого нет права
    # обходить политики.
    queue_login = f"dodo_queue_{pid}"
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

    def queue_dsn(db: str) -> str:
        return make_conninfo(
            "", host=host, port=port, dbname=db, user=queue_login, password=QUEUE_PASSWORD
        )

    with admin:
        admin.execute(f'drop database if exists "{dbname}" with (force)')
        for role in (owner, app_login, queue_login):
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
        admin.execute(
            f"""create role "{queue_login}" login password '{QUEUE_PASSWORD}'
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
        # То же самое для роли очереди: на чистой площадке её заводит миграция,
        # на общем кластере она уже есть, и её выдаёт администратор.
        admin.execute(f'grant queue_worker to "{owner}" with admin option')

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
            # Логин-роль очереди состоит в двух ролях: `queue_worker` — разбирать
            # очередь, `app_user` — работать с данными внутри задачи. Членства в
            # `queue_worker` у роли веба нет намеренно: тогда защита полезной
            # нагрузки (`0047`) держалась бы на дисциплине кода.
            grants.execute(f'grant connect on database "{dbname}" to "{queue_login}"')
            grants.execute(f'grant app_user to "{queue_login}"')
            grants.execute(f'grant queue_worker to "{queue_login}"')
        yield {
            "dbname": dbname,
            "owner_dsn": owner_dsn(dbname),
            "app_dsn": app_dsn(dbname),
            "queue_dsn": queue_dsn(dbname),
        }
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as cleanup:
            cleanup.execute(f'drop database if exists "{dbname}" with (force)')
            for role in (owner, app_login, queue_login):
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
    """Роли с неполным набором регистров не видно строк скрытого от неё.

    Роль здесь — управляющий точки: после D036 набор бухгалтера полон, и её
    отказ ничего бы не доказывал. Управляющему не виден внутренний регистр
    (D031), и проверка стоит на нём.
    """
    with as_app_user(app_conn, USER_MANAGER) as conn:
        seen = {row[0] for row in conn.execute("select ledger from pay_components").fetchall()}
    assert seen == {"official"}, seen


def test_the_application_can_still_write(app_conn, plain_owner):
    """Не только чтение: запись под контекстом тоже не должна требовать обхода.

    Вставка тянет за собой `with check` четырёх таблиц и триггер, который
    дописывает набор регистров в строку ведомости, — то есть весь путь, каким
    расчёт кладёт результат.

    Раньше срабатывание триггера проверялось последствием: бухгалтеру не видны
    итоги строки дополнительного регистра. После T071 итоги ей не видны никогда
    и ни в какой строке, так что такая проверка стала бы пустой — зелёной и при
    молча не сработавшем триггере. Поэтому колонка читается прямо, соединением
    администратора: роли приложения она закрыта нарочно (T065). Ради этого
    написанное приходится закоммитить (в этом модуле `as_app_user` всё
    откатывает) и убрать за собой руками.
    """
    import psycopg
    from psycopg.conninfo import make_conninfo

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
        conn.commit()  # иначе администратору нечего будет прочитать

    admin_dsn = make_conninfo(ADMIN_DSN, dbname=plain_owner["dbname"])
    try:
        with psycopg.connect(admin_dsn) as admin:
            # Без регистрации доменных типов массив `ledger[]` приезжает одной
            # строкой — на это уже наступали, см. `core/db_types.py`.
            from core.db_types import register_enum_types

            register_enum_types(admin)
            ledgers = admin.execute(
                "select ledgers from payslips where id = %s", (payslip,)
            ).fetchone()[0]
        assert ledgers == ["supplementary"], (
            f"триггер не заполнил набор регистров строки: {ledgers}"
        )
    finally:
        # База модульная, и следующие тесты считают строки — уносим за собой.
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("delete from payslip_totals where payslip_id = %s", (payslip,))
            admin.execute("delete from pay_components where payslip_id = %s", (payslip,))
            employee = admin.execute(
                "select employee_id from payslips where id = %s", (payslip,)
            ).fetchone()[0]
            admin.execute("delete from payslips where id = %s", (payslip,))
            admin.execute("delete from employees where id = %s", (employee,))


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
    # Политик на memberships с `0242` три: чтение своего, чтение чужого тем, кто
    # ведёт роли, и запись под тем же правом. Прежний вид — одна политика через
    # `app_tenant_ids()`, поэтому на время снимаются все три.
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        for name in ("own_membership_read", "memberships_manage_read", "memberships_manage_write"):
            admin.execute(f"drop policy {name} on memberships")
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
                create policy own_membership_read on memberships
                    for select
                    using (user_id = app_user_id())
            """)
            admin.execute("""
                create policy memberships_manage_read on memberships
                    for select
                    using (app_has_permission(tenant_id, 'roles.manage'))
            """)
            admin.execute("""
                create policy memberships_manage_write on memberships
                    for all
                    using (app_has_permission(tenant_id, 'roles.manage'))
                    with check (app_has_permission(tenant_id, 'roles.manage'))
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


# --- Роли площадки: миграции, продукт, очередь (T124, issues #66 и #50) -------
# На площадке ролей три, и разведены они не для красоты: схему накатывает
# владелец, продукт ходит логин-ролью без DDL, очередь — своей. Пока обе службы
# ходили одной ролью, рабочий процесс очереди не работал вовсе
# (`permission denied for table django_q_ormq`), и узнать об этом можно было
# только на площадке.


def test_the_queue_login_role_can_work_the_queue(plain_owner):
    """Логин-роль очереди разбирает очередь — на конфигурации площадки.

    Проверяется настоящим подключением этой ролью, а не привилегией в каталоге:
    привилегия бывает выдана роли, а логин-роль не окажется её членом — и
    рабочий процесс всё равно упадёт на площадке.
    """
    import psycopg

    with psycopg.connect(plain_owner["queue_dsn"]) as conn:
        conn.execute("select payload from django_q_ormq").fetchall()
        conn.execute(
            "insert into django_q_ormq (key, payload, lock) values ('t', 'x', now())"
        )
        conn.execute("delete from django_q_ormq where key = 't'")
        conn.commit()


def test_the_queue_login_role_still_obeys_the_policies(plain_owner):
    """Она же не получает данных продукта в обход политик.

    Роль очереди подключается к базе с ФИО и суммами, и своей дороги к ним у
    неё быть не должно: контекст пользователя она выставляет уже внутри задачи
    (`db_context` делает `set local role app_user`). Без контекста — пусто, как
    у любой другой роли.
    """
    import psycopg

    with psycopg.connect(plain_owner["queue_dsn"]) as conn:
        assert conn.execute("select count(*) from employees").fetchone()[0] == 0
        assert conn.execute("select count(*) from pay_components").fetchone()[0] == 0


def test_the_web_login_role_cannot_read_the_queue(plain_owner):
    """А роль веба очередь по-прежнему не читает — иначе `0047` отменена.

    Это и есть цена решения: очередь получила свою роль, а не права `app_user`.
    Если однажды логин-роль веба сделают членом `queue_worker` «чтобы работало»,
    красным станет этот тест, а не тишина.
    """
    import psycopg

    with psycopg.connect(plain_owner["app_dsn"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select payload from django_q_ormq").fetchall()
