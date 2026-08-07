from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from payroll import PayrollEngine, load_preset
from payroll.importers import read_plata

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

# Обезличенный файл, лежит в репозитории: те же восемь листов и четыре схемы,
# выдуманные люди. Пересоздаётся `python tools/make_fixture.py`.
PLATA_SAMPLE = FIXTURES / "plata-sample.xlsx"

# Настоящая таблица бухгалтерии. В репозитории её нет и не будет: ФИО и суммы
# живых людей. Путь задаётся переменной PAYROLL_FIXTURE, тесты сверки без неё
# пропускаются.
PLATA_REAL = os.environ.get("PAYROLL_FIXTURE")


@pytest.fixture(scope="session")
def serbia_preset() -> dict:
    return load_preset("serbia-2026")


@pytest.fixture(scope="session")
def engine(serbia_preset) -> PayrollEngine:
    return PayrollEngine(serbia_preset)


@pytest.fixture(scope="session")
def sample_rows():
    """Обезличенный набор из репозитория: регрессия на поведение движка."""
    if not PLATA_SAMPLE.exists():
        pytest.fail(
            f"нет {PLATA_SAMPLE} — пересоздайте: python tools/make_fixture.py"
        )
    return read_plata(PLATA_SAMPLE)


@pytest.fixture(scope="session")
def june_rows():
    """Настоящая таблица бухгалтерии Сербии за июнь 2026 — сверка с реальностью."""
    if not PLATA_REAL:
        pytest.skip("не задан PAYROLL_FIXTURE — сверка с настоящей таблицей пропущена")
    path = Path(PLATA_REAL)
    if not path.exists():
        pytest.skip(f"PAYROLL_FIXTURE указывает на несуществующий файл: {path}")
    return read_plata(path)


# =============================================================================
# Тесты схемы: живой Postgres
# =============================================================================
# Разграничение доступа живёт в SQL, поэтому проверять его можно только на
# настоящей базе. Тесты сами создают временную БД, накатывают миграции Django
# и удаляют её за собой. Нет доступного Postgres — тесты скипаются, остальной
# прогон остаётся зелёным.
#
# Куда подключаться: DODO_TEST_ADMIN_DSN, по умолчанию локальный кластер.

MANAGE_PY = ROOT / "manage.py"

ADMIN_DSN = os.environ.get("DODO_TEST_ADMIN_DSN", "postgresql:///postgres")

# Фиксированные id: в ассертах читаемее, чем случайные uuid
T1 = "11111111-1111-1111-1111-111111111111"       # тенант Сербия
T2 = "11111111-1111-1111-1111-111111111112"       # второй партнёр, для проверки изоляции
LE1 = "22222222-2222-2222-2222-222222222221"
LE2 = "22222222-2222-2222-2222-222222222222"      # второе юрлицо того же тенанта
U_BG1 = "a1111111-0000-0000-0000-000000000001"
U_NS1 = "a1111111-0000-0000-0000-000000000002"
U_NS2 = "a1111111-0000-0000-0000-000000000003"
U_OTHER = "a1111111-0000-0000-0000-00000000000f"  # точка второго тенанта
I_REVENUE = "b1111111-0000-0000-0000-000000000001"
I_FOOD = "b1111111-0000-0000-0000-000000000002"
I_UTILITIES = "b1111111-0000-0000-0000-000000000003"
I_LABOUR = "b1111111-0000-0000-0000-000000000004"
I_TAXES = "b1111111-0000-0000-0000-000000000005"
I_TOTAL = "b1111111-0000-0000-0000-000000000006"
I_TRANSFER = "b1111111-0000-0000-0000-000000000007"
CP_EPS = "c1111111-0000-0000-0000-000000000001"   # поставщик электричества
CP_METRO = "c1111111-0000-0000-0000-000000000002"
USER_DIRECTOR = "d1111111-0000-0000-0000-000000000001"    # видит все регистры и точки
USER_ACCOUNTANT = "d1111111-0000-0000-0000-000000000002"  # только белый регистр
USER_MANAGER = "d1111111-0000-0000-0000-000000000003"     # только точка NS1
USER_OTHER = "d1111111-0000-0000-0000-00000000000f"       # второй тенант
JUNE = "2026-06-01"
JULY = "2026-07-01"


def run_manage(dsn: str, *args: str) -> subprocess.CompletedProcess:
    """Выполнить команду Django на указанной базе.

    Через подпроцесс, а не импортом: так тест проверяет ровно то, что запустит
    человек руками, вместе с настройками и каркасом проекта.
    """
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "SECRET_KEY": "test-only-not-a-secret",
        "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    if env.get("COVERAGE_PROCESS_START"):
        # Покрытие подпроцесса включается только под coverage — в обычном
        # прогоне лишнего каталога в PYTHONPATH не появляется.
        hook = str(Path(__file__).parent / "_coverage_subprocess")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [hook, env.get("PYTHONPATH", "")]))
    return subprocess.run(
        [sys.executable, str(MANAGE_PY), *args],
        env=env, check=True, capture_output=True, text=True,
    )


@contextmanager
def temp_database(suffix: str):
    """Временная база с накатанной схемой. Удаляется в любом случае."""
    psycopg = pytest.importorskip("psycopg", reason="нужен psycopg: pip install -e '.[dev]'")
    from psycopg.conninfo import make_conninfo

    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"нет доступного Postgres по {ADMIN_DSN}: {exc}")

    dbname = f"dodo_pnl_test_{suffix}_{os.getpid()}"
    with admin:
        admin.execute(f'drop database if exists "{dbname}"')
        admin.execute(f'create database "{dbname}"')

    dsn = make_conninfo(ADMIN_DSN, dbname=dbname)
    try:
        run_manage(dsn, "migrate", "--no-input")
        yield dsn
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin2:
            admin2.execute(f'drop database if exists "{dbname}" with (force)')


@pytest.fixture(scope="session")
def pg_dsn():
    """База под тесты доступа. Создаётся один раз на прогон."""
    with temp_database("rls") as dsn:
        yield dsn


@pytest.fixture
def db(pg_dsn):
    """Соединение на один тест. Всё, что тест написал, откатывается."""
    import psycopg

    from core.db_types import register_enum_types

    conn = psycopg.connect(pg_dsn)
    register_enum_types(conn)
    try:
        _seed(conn)
        yield conn
    finally:
        conn.rollback()
        conn.close()


R_DIRECTOR = "e1111111-0000-0000-0000-000000000001"
R_ACCOUNTANT = "e1111111-0000-0000-0000-000000000002"
R_MANAGER = "e1111111-0000-0000-0000-000000000003"
R_SYSTEM = "e1111111-0000-0000-0000-000000000010"  # роль без тенанта, общая для всех
R_OTHER = "e1111111-0000-0000-0000-00000000000f"


def _seed(conn) -> None:
    """Минимальный, но живой набор: два тенанта, три точки, статьи, роли.

    Три точки нужны специально: на трёх видно распределение копеек при делении
    поровну, на двух оно не проявляется.
    """
    conn.execute(
        """insert into tenants (id, code, title, country_code, base_currency, report_currency)
           values (%s, 'rs', 'Dodo Serbia', 'RS', 'RSD', 'EUR'),
                  (%s, 'xx', 'Другой партнёр', 'XX', 'XXX', 'EUR')""",
        (T1, T2),
    )
    conn.execute(
        """insert into legal_entities (id, tenant_id, title)
           values (%s, %s, 'Dodo RS d.o.o.'), (%s, %s, 'Dodo RS Two d.o.o.')""",
        (LE1, T1, LE2, T1),
    )
    conn.execute(
        """insert into units (id, tenant_id, legal_entity_id, code, title, opened_at) values
               (%s, %s, %s, 'BG1', 'Beograd 1', '2023-01-01'),
               (%s, %s, %s, 'NS1', 'Bulevar',   '2023-01-01'),
               (%s, %s, %s, 'NS2', 'Dunavska',  '2023-01-01')""",
        (U_BG1, T1, LE1, U_NS1, T1, LE1, U_NS2, T1, LE1),
    )
    conn.execute(
        "insert into units (id, tenant_id, code, title) values (%s, %s, 'ZZ1', 'Чужая точка')",
        (U_OTHER, T2),
    )
    conn.execute(
        """insert into pnl_items (id, tenant_id, code, title, kind, sort_order) values
               (%s, null, 'revenue',       'Выручка',           'revenue',  10),
               (%s, null, 'food_cost',     'Себестоимость',     'expense',  20),
               (%s, null, 'utilities',     'Коммунальные',      'expense',  30),
               (%s, null, 'labour_cost',   'Зарплата',          'expense',  40),
               (%s, null, 'payroll_taxes', 'Налоги с зарплаты', 'expense',  50),
               (%s, null, 'total',         'Результат',         'subtotal', 90)""",
        (I_REVENUE, I_FOOD, I_UTILITIES, I_LABOUR, I_TAXES, I_TOTAL),
    )
    conn.execute(
        """insert into counterparties (id, tenant_id, title)
           values (%s, %s, 'EPS Elektro'), (%s, %s, 'Metro')""",
        (CP_EPS, T1, CP_METRO, T1),
    )
    conn.execute(
        """insert into roles (id, tenant_id, code, title, visible_ledgers) values
               (%s, %s,   'director',   'Оперативный директор',
                   '{official,supplementary,internal}'),
               (%s, %s,   'accountant', 'Бухгалтер',            '{official}'),
               (%s, %s,   'manager',    'Управляющий точки',    '{official,supplementary}'),
               (%s, null, 'support',    'Поддержка сервиса',    '{official}'),
               (%s, %s,   'director',   'Директор партнёра',
                   '{official,supplementary,internal}')""",
        (R_DIRECTOR, T1, R_ACCOUNTANT, T1, R_MANAGER, T1, R_SYSTEM, R_OTHER, T2),
    )
    conn.execute(
        """insert into memberships (tenant_id, user_id, role_id, unit_ids) values
               (%s, %s, %s, null),
               (%s, %s, %s, null),
               (%s, %s, %s, array[%s]::uuid[]),
               (%s, %s, %s, null)""",
        (
            T1, USER_DIRECTOR, R_DIRECTOR,
            T1, USER_ACCOUNTANT, R_ACCOUNTANT,
            T1, USER_MANAGER, R_MANAGER, U_NS1,
            T2, USER_OTHER, R_OTHER,
        ),
    )
    conn.execute(
        "insert into periods (tenant_id, period, status) values (%s, %s, 'open')", (T1, JUNE)
    )
    conn.execute(
        """insert into fx_rates (base_currency, quote_currency, rate_date, rate)
           values ('RSD', 'EUR', '2026-06-30', 0.00854)"""
    )


# =============================================================================
# Живой Django поверх временной базы с сидом
# =============================================================================
# Настройки Django читаются один раз на процесс, поэтому база у всех веб-тестов
# общая — фикстура сессионная и живёт здесь, а не в одном из модулей. Тесты,
# которым важны суммы, обязаны сначала привести зарплатные таблицы в известное
# состояние: расчёт (`test_payrun`) и ручные компоненты (`test_web`) пишут в
# одни и те же `payruns`, и порядок модулей не гарантирован.


@pytest.fixture(scope="session")
def web_env():
    """Временная база с миграциями и сидом + настроенный Django в этом процессе."""
    with temp_database("web") as dsn:
        run_manage(dsn, "seed_dev")

        os.environ["DATABASE_URL"] = dsn
        os.environ.setdefault("SECRET_KEY", "test-only-not-a-secret")
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

        import django
        from django.test.utils import setup_test_environment, teardown_test_environment

        django.setup()
        setup_test_environment()
        try:
            yield dsn
        finally:
            from django.db import connection

            connection.close()
            teardown_test_environment()


@pytest.fixture
def client(web_env):
    from django.test import Client

    return Client()


def login_as(client, code: str):
    return client.post("/dev/login/", {"user": code})


def body(response) -> str:
    return response.content.decode()


def period_url(client) -> str:
    """Ссылка на страницу периода со списка — так же, как её берёт человек."""
    import re

    html = body(client.get("/periods/"))
    match = re.search(r'href="(/periods/[0-9a-f-]+/)"', html)
    assert match, f"на списке периодов нет ссылки на период:\n{html}"
    return match.group(1)


def wipe_payruns(dsn: str) -> None:
    """Снести все расчёты тенанта сида — суперпользователем, мимо политик.

    Нужно тестам, которые проверяют конкретные суммы: без этого их результат
    зависел бы от того, успел ли соседний модуль посчитать период.
    """
    import psycopg

    # Порядок явный: `on delete` Django во внешние ключи не пишет (он исполняет
    # каскад в Python), поэтому в чистом SQL каскада нет — см. журнал блока db.
    with psycopg.connect(dsn, autocommit=True) as conn:
        tenants = "(select id from tenants where code = 'rs-dev')"
        conn.execute(f"delete from pay_components where tenant_id in {tenants}")
        conn.execute(f"delete from payslip_totals where tenant_id in {tenants}")
        conn.execute(f"delete from payslips where tenant_id in {tenants}")
        conn.execute(f"delete from payruns where tenant_id in {tenants}")


@contextmanager
def as_app_user(conn, user_id: str | None):
    """Работать ролью приложения, чтобы RLS вообще действовала.

    Тесты подключаются владельцем схемы. На владельца политики не действуют
    (а суперпользователь обходит даже `force row level security`), поэтому
    любой тест доступа обязан переключиться на `app_user` — ровно так, как это
    делает сервис. `user_id = None` — проверка «контекст не выставлен».
    """
    conn.execute("set local role app_user")
    conn.execute("select set_config('app.user_id', %s, true)", (user_id or "",))
    try:
        yield conn
    finally:
        conn.execute("reset role")
        conn.execute("select set_config('app.user_id', '', true)")


def pay_component(conn, *, ledger: str, amount: str = "1000.00", code: str = "hours.regular",
                  tenant: str = T1) -> str:
    """Компонент выплаты нужного регистра — материал для проверки видимости."""
    employee_id = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Тестов') returning id""",
        (tenant, f"ext-{ledger}-{code}-{amount}"),
    ).fetchone()[0]
    payrun_id = conn.execute(
        """insert into payruns (tenant_id, period) values (%s, %s)
           on conflict (tenant_id, period) do update set period = excluded.period
           returning id""",
        (tenant, JUNE),
    ).fetchone()[0]
    payslip_id = conn.execute(
        """insert into payslips (tenant_id, payrun_id, employee_id)
           values (%s, %s, %s) returning id""",
        (tenant, payrun_id, employee_id),
    ).fetchone()[0]
    return conn.execute(
        """insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
           values (%s, %s, %s, %s, %s, %s) returning id""",
        (tenant, payslip_id, code, "Часы", amount, ledger),
    ).fetchone()[0]
