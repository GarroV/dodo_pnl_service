from __future__ import annotations

import json
import os
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
# Разнесение, RLS и идемпотентность живут в SQL, поэтому проверять их можно
# только на настоящей базе. Тесты сами создают временную БД, накатывают
# миграции и удаляют её за собой. Нет доступного Postgres — тесты скипаются,
# остальной прогон остаётся зелёным.
#
# Куда подключаться: DODO_TEST_ADMIN_DSN, по умолчанию локальный кластер.

PLATFORM_SQL = ROOT / "db" / "platform" / "postgres.sql"
MIGRATIONS_DIR = ROOT / "db" / "migrations"

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
USER_DIRECTOR = "d1111111-0000-0000-0000-000000000001"    # видит все слои и точки
USER_ACCOUNTANT = "d1111111-0000-0000-0000-000000000002"  # только белый слой
USER_MANAGER = "d1111111-0000-0000-0000-000000000003"     # только точка NS1
USER_OTHER = "d1111111-0000-0000-0000-00000000000f"       # второй тенант
JUNE = "2026-06-01"
JULY = "2026-07-01"


def _sql_files() -> list[Path]:
    return [PLATFORM_SQL, *sorted(MIGRATIONS_DIR.glob("*.sql"))]


@pytest.fixture(scope="session")
def pg_dsn():
    """Временная БД со накатанной схемой. Создаётся один раз на прогон."""
    psycopg = pytest.importorskip("psycopg", reason="нужен psycopg: pip install -e '.[dev]'")
    from psycopg.conninfo import make_conninfo

    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"нет доступного Postgres по {ADMIN_DSN}: {exc}")

    dbname = f"dodo_pnl_test_{os.getpid()}"
    with admin:
        admin.execute(f'drop database if exists "{dbname}"')
        admin.execute(f'create database "{dbname}"')

    dsn = make_conninfo(ADMIN_DSN, dbname=dbname)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for path in _sql_files():
                conn.execute(path.read_text())
        yield dsn
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin2:
            admin2.execute(f'drop database if exists "{dbname}" with (force)')


@pytest.fixture
def db(pg_dsn):
    """Соединение на один тест. Всё, что тест написал, откатывается."""
    import psycopg

    conn = psycopg.connect(pg_dsn)
    try:
        _seed(conn)
        yield conn
    finally:
        conn.rollback()
        conn.close()


R_DIRECTOR = "e1111111-0000-0000-0000-000000000001"
R_ACCOUNTANT = "e1111111-0000-0000-0000-000000000002"
R_MANAGER = "e1111111-0000-0000-0000-000000000003"
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
               (%s, null, 'total',         'Результат',         'subtotal', 90),
               (%s, null, 'cash_transfer', 'Переводы',          'transfer', 95)""",
        (I_REVENUE, I_FOOD, I_UTILITIES, I_LABOUR, I_TAXES, I_TOTAL, I_TRANSFER),
    )
    conn.execute(
        """insert into counterparties (id, tenant_id, title)
           values (%s, %s, 'EPS Elektro'), (%s, %s, 'Metro')""",
        (CP_EPS, T1, CP_METRO, T1),
    )
    conn.execute(
        """insert into roles (id, tenant_id, code, title, visible_layers) values
               (%s, %s, 'director',   'Оперативный директор', '{white,grey,black}'),
               (%s, %s, 'accountant', 'Бухгалтер',            '{white}'),
               (%s, %s, 'manager',    'Управляющий точки',    '{white,grey}'),
               (%s, %s, 'director',   'Директор партнёра',    '{white,grey,black}')""",
        (R_DIRECTOR, T1, R_ACCOUNTANT, T1, R_MANAGER, T1, R_OTHER, T2),
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


@contextmanager
def as_user(conn, user_id: str):
    """Работать от лица пользователя приложения, чтобы RLS действовала.

    Тесты подключаются владельцем схемы, а на владельца политики не действуют.
    Поэтому переключаемся на роль приложения — ровно как это делает сервис.
    """
    conn.execute("set local role pnl_app")
    conn.execute("select set_config('app.user_id', %s, true)", (user_id,))
    try:
        yield conn
    finally:
        conn.execute("reset role")
        conn.execute("select set_config('app.user_id', '', true)")


def upsert_fact(conn, **payload) -> tuple[str, str]:
    """Записать факт так, как это делает импортёр. Возвращает (id, действие)."""
    raw = json.dumps(payload, default=str)
    row = conn.execute("select fact_id, action from upsert_fact(%s::jsonb)", (raw,)).fetchone()
    return row[0], row[1]


def invoice_line(conn, *, dedup_key, amount, counterparty=CP_EPS, period=JUNE,
                 pnl_item=I_UTILITIES, legal_entity=LE1, layer="white", **extra) -> str:
    """Позиция фактуры на юрлицо: точка ещё неизвестна, ждёт разнесения."""
    payload = dict(
        tenant_id=T1,
        period=period,
        doc_date="2026-07-05",   # счёт за июнь приходит в июле
        legal_entity_id=legal_entity,
        pnl_item_id=pnl_item,
        counterparty_id=counterparty,
        amount=amount,
        currency="RSD",
        title="Электричество",
        source="einvoice",
        dedup_key=dedup_key,
        allocation="pending",
        layer=layer,
    )
    payload.update(extra)
    fact_id, _ = upsert_fact(conn, **payload)
    return fact_id


def revenue_fact(conn, *, unit, amount, period=JUNE, dedup_key=None) -> str:
    """Выручка точки: приходит из Dodo IS сразу с точкой."""
    fact_id, _ = upsert_fact(
        conn,
        tenant_id=T1, period=period, doc_date=period, unit_id=unit,
        legal_entity_id=LE1, pnl_item_id=I_REVENUE, amount=amount, currency="RSD",
        title="Выручка", source="dodo_is",
        dedup_key=dedup_key or f"dodo_is:revenue:{unit}:{period}",
        allocation="direct",
    )
    return fact_id
