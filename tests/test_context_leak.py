"""
Утечка контекста между запросами и в фоновых задачах (T015).

Главный риск блока: контекст пользователя выставляется в соединение, а
соединения переиспользуются. Если он переживёт запрос, следующий пользователь
того же соединения увидит чужие данные — и ни одна проверка в интерфейсе этого
не заметит, потому что с точки зрения кода всё правильно.

Проверяются оба случая из контракта:

- два запроса разных пользователей **на одном и том же соединении** (тест сам
  доказывает, что соединение то же: сравнивает backend pid, иначе проверка
  ничего не значила бы);
- фоновая задача, у которой HTTP-запроса нет вовсе.

Оба обязаны давать пустоту, а не чужие строки.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import body, period_url, wipe_payruns

JUNE = "2026-06-01"

AMOUNT_OFFICIAL = Decimal("444000.00")
AMOUNT_INTERNAL = Decimal("55500.00")


def login_real(client, username: str, password: str):
    return client.post("/login/", {"username": username, "password": password})


def backend_pid() -> int | None:
    """Номер процесса Postgres на том конце соединения Django.

    Меняется — значит соединение переоткрылось, и тест про «одно соединение»
    ничего не доказывает.
    """
    from django.db import connection

    if connection.connection is None:
        return None
    return connection.connection.info.backend_pid


def guc_and_role() -> tuple[str | None, str]:
    """Что осталось в соединении: контекст пользователя и текущая роль."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("select current_setting('app.user_id', true), current_user")
        return cur.fetchone()


@pytest.fixture
def seed_password(web_env) -> str:
    from core.management.commands.seed_dev import SEED_PASSWORD

    return SEED_PASSWORD


@pytest.fixture
def pooled_connection(web_env):
    """Соединение, которое переживает запрос, — то самое, где возможна утечка.

    По умолчанию Django закрывает соединение в конце запроса, и проверка
    «второй запрос на том же соединении» оказалась бы фиктивной: соединение
    было бы новым, чистым по построению и без всякой заслуги нашего кода.
    """
    from django.db import connection

    was = connection.settings_dict.get("CONN_MAX_AGE")
    connection.close()
    connection.settings_dict["CONN_MAX_AGE"] = 60
    try:
        yield connection
    finally:
        connection.close()
        connection.settings_dict["CONN_MAX_AGE"] = was


@pytest.fixture
def two_ledger_rows(web_env):
    """Официальная и внутренняя строки: разным ролям видно разное.

    Сотрудник и строка ведомости явно привязаны к точке NS1 — точке
    управляющего (см. `test_next_user_on_the_same_connection_sees_only_his_own`).
    Без этого случайно выбранный сотрудник другой точки пропал бы у управляющего
    по видимости точки (T044/T057), и проверка вышла бы зелёной не по причине
    регистров, а по причине, которую тест не собирался проверять.
    """
    import psycopg

    wipe_payruns(web_env)
    with psycopg.connect(web_env, autocommit=True) as conn:
        tenant = conn.execute("select id from tenants where code = 'rs-dev'").fetchone()[0]
        unit_ns1 = conn.execute(
            "select id from units where tenant_id = %s and code = 'NS1'", (tenant,)
        ).fetchone()[0]
        employee = conn.execute(
            """select e.id from employees e
                 join employment_terms t on t.employee_id = e.id
                where e.tenant_id = %s and t.unit_id = %s
                order by e.external_id limit 1""",
            (tenant, unit_ns1),
        ).fetchone()[0]
        payrun = conn.execute(
            """insert into payruns (tenant_id, period) values (%s, %s)
               on conflict (tenant_id, period) do update set period = excluded.period
               returning id""",
            (tenant, JUNE),
        ).fetchone()[0]
        payslip = conn.execute(
            """insert into payslips (tenant_id, payrun_id, employee_id, unit_id)
               values (%s, %s, %s, %s) returning id""",
            (tenant, payrun, employee, unit_ns1),
        ).fetchone()[0]
        for ledger, amount in (
            ("official", AMOUNT_OFFICIAL),
            ("internal", AMOUNT_INTERNAL),
        ):
            conn.execute(
                """insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
                   values (%s, %s, %s, %s, %s, %s)""",
                (tenant, payslip, f"hours.{ledger}", "Часы", amount, ledger),
            )
    return None


# --- два запроса на одном соединении -----------------------------------------


def test_context_does_not_outlive_the_request(pooled_connection, seed_password):
    """После запроса в соединении не остаётся ни пользователя, ни роли приложения."""
    from django.test import Client

    client = Client()
    login_real(client, "director", seed_password)
    assert client.get("/periods/").status_code == 200
    assert backend_pid() is not None, "соединение закрылось — проверка фиктивна"

    user, role = guc_and_role()
    assert user in ("", None), f"контекст пережил запрос: {user}"
    assert role != "app_user", f"роль пережила запрос: {role}"


def test_next_user_on_the_same_connection_sees_only_his_own(
    pooled_connection, seed_password, two_ledger_rows
):
    """Директор, затем управляющий по тому же соединению: чужого регистра не видно.

    Была роль бухгалтера — после D036 набор её регистров полон, как у
    директора, и «внутренний регистр не протёк» перестало бы что-либо
    доказывать (она увидела бы его законно). Управляющему точки внутренний
    регистр не открыт (D031), а строка сида — на его же точке NS1
    (`two_ledger_rows`), поэтому отказ здесь именно от регистра, а не от того,
    что точка другая.
    """
    from django.test import Client

    from web.format import money

    director = Client()
    login_real(director, "director", seed_password)
    url = period_url(director)
    seen = body(director.get(url))
    pid_first = backend_pid()
    assert money(AMOUNT_INTERNAL) in seen, "директору внутренний регистр должен быть виден"

    manager = Client()
    login_real(manager, "manager", seed_password)
    seen = body(manager.get(url))
    assert backend_pid() == pid_first, "соединение переоткрылось — тест ничего не доказывает"

    assert money(AMOUNT_OFFICIAL) in seen, "официальный регистр управляющему открыт (D031)"
    assert money(AMOUNT_INTERNAL) not in seen, "внутренний регистр протёк на управляющего"


def test_anonymous_after_a_logged_in_user_gets_nothing(
    pooled_connection, seed_password, two_ledger_rows
):
    """Классическая утечка: после вошедшего по тому же соединению приходит гость."""
    from django.test import Client

    from web.format import money

    director = Client()
    login_real(director, "director", seed_password)
    url = period_url(director)
    assert money(AMOUNT_INTERNAL) in body(director.get(url))
    pid_first = backend_pid()

    guest = Client()
    response = guest.get(url)
    assert backend_pid() == pid_first, "соединение переоткрылось — тест ничего не доказывает"
    assert response.status_code == 302
    assert money(AMOUNT_INTERNAL) not in body(guest.get("/login/"))


# --- фоновая задача ----------------------------------------------------------


def job_counts() -> dict[str, int]:
    """То, что «увидела» бы фоновая задача: строки нескольких таблиц продукта."""
    from core.models import Employee, PayComponent, Period, Tenant

    return {
        "tenants": Tenant.objects.count(),
        "periods": Period.objects.count(),
        "employees": Employee.objects.count(),
        "components": PayComponent.objects.count(),
    }


def test_background_job_without_context_gets_nothing(web_env, two_ledger_rows):
    """Задача забыла сказать, от чьего имени работает, — она не видит ничего."""
    from web.dbcontext import db_context

    with db_context(None):
        counts = job_counts()
    assert set(counts.values()) == {0}, counts


def test_background_job_with_context_sees_its_own(web_env, two_ledger_rows):
    """Контроль: с выставленным пользователем строки на месте.

    Без него предыдущий тест был бы зелёным и на пустой базе.
    """
    from core.management.commands.seed_dev import det_id
    from web.dbcontext import db_context

    with db_context(det_id("user", "director")):
        counts = job_counts()
    assert counts["tenants"] == 1
    assert counts["employees"] > 0
    assert counts["components"] == 2


def test_background_job_leaves_no_context_behind(web_env):
    """Контекст задачи не достаётся следующей: он снимается вместе с транзакцией."""
    from core.management.commands.seed_dev import det_id
    from web.dbcontext import db_context

    with db_context(det_id("user", "director")):
        pass
    user, role = guc_and_role()
    assert user in ("", None)
    assert role != "app_user"


def test_a_job_that_skips_the_helper_is_not_protected_by_anything(web_env, two_ledger_rows):
    """Ловушка, которую тесты обязаны показывать, а не прятать.

    Запрос мимо `db_context` идёт той ролью, которой подключились. В разработке
    и в нынешнем образе это владелец схемы (он же суперпользователь), а его RLS
    не ограничивает — то есть фоновая задача, забывшая помощник, увидит всё.
    Именно поэтому `db_context` для кода без HTTP-запроса обязателен, а роль
    подключения не должна быть владельцем (issue #44, задача T052 блока db).
    """
    assert job_counts()["components"] == 2
