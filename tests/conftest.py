from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo

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


def test_db_name(suffix: str) -> str:
    """Имя временной базы прогона. Одно место, потому что имён два потребителя.

    Базу заводит `temp_database`, а адрес той же базы нужен настройке Django
    **до** первой фикстуры (см. ниже). Два места, где это имя собирается
    строкой, разъехались бы молча: Django ходил бы в одну базу, а миграции
    накатывались бы в другую.
    """
    return f"dodo_pnl_test_{suffix}_{os.getpid()}"


# Адрес базы веб-тестов известен заранее — он зависит только от номера процесса.
WEB_DSN = make_conninfo(ADMIN_DSN, dbname=test_db_name("web"))


def _configure_django() -> None:
    """Настроить Django один раз, при сборе тестов, а не побочным эффектом.

    Раньше это делала фикстура `web_env`, и работало оно только потому, что
    веб-тесты собираются раньше «чистых». Файл, где базы нет вовсе, но код зовёт
    `gettext` (сверка, пороги расхождений), запущенный **в одиночку** падал
    шестью проверками с `ImproperlyConfigured: Requested setting USE_I18N` —
    issue #79. Настройка, приезжающая из чужой фикстуры, и есть зависимость от
    порядка сбора: сегодня она красит исправный код, завтра молча выключает
    проверку.

    Адрес базы подставляется тот же, что заведёт `web_env`: имя временной базы
    зависит только от номера процесса и потому известно заранее (см.
    `test_db_name`). Иначе настройки Django замёрзли бы на адресе из окружения
    разработчика — то есть веб-тесты пошли бы в его рабочую базу.

    Подключения здесь не открывается: `django.setup()` только читает настройки
    и поднимает приложения. Нет Postgres — «чистые» тесты по-прежнему идут,
    а тесты схемы по-прежнему скипаются.
    """
    import django

    os.environ["DATABASE_URL"] = WEB_DSN
    os.environ.setdefault("SECRET_KEY", "test-only-not-a-secret")
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    # Рабочего процесса очереди в прогоне тестов нет, поэтому стенд настроен
    # как стенд без очереди: расчёт считается прямо в запросе (T024). Это не
    # обход фонового пути, а его выключатель — тот самый, которым продукт
    # переводится в синхронный режим на площадке без рабочего процесса.
    # Сам фоновый путь проверяется в `test_payrun_jobs.py`: там задача
    # запускается напрямую, а постановка в очередь — под
    # `override_settings(PAYRUN_BACKGROUND=True)`.
    os.environ["PAYRUN_BACKGROUND"] = "0"
    django.setup()


_configure_django()

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
USER_ACCOUNTANT = "d1111111-0000-0000-0000-000000000002"  # все регистры (D036)
# Роль с НЕПОЛНЫМ набором регистров в фикстуре ровно одна — управляющий: он
# видит официальный и дополнительный, но не внутренний (D031). После D036 все
# проверки видимости регистров стоят на нём, а не на бухгалтере: набор
# бухгалтера полон, и отказ ему нельзя было бы отличить от отсутствия защиты.
USER_MANAGER = "d1111111-0000-0000-0000-000000000003"     # точка NS1, без внутреннего
USER_ADMIN = "d1111111-0000-0000-0000-000000000004"       # видит всё, данных не правит
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

    dbname = test_db_name(suffix)
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
R_ADMIN = "e1111111-0000-0000-0000-000000000004"  # все регистры и точки, но данных не правит
R_SYSTEM = "e1111111-0000-0000-0000-000000000010"  # роль без тенанта, общая для всех
R_OTHER = "e1111111-0000-0000-0000-00000000000f"

# Права ролей. Ими закрыты действия, которые от видимости не зависят: право
# считать период и править табель проверяется отдельно от набора регистров
# (T064). У администратора сети их нет намеренно — и регистры ему в фикстуре
# выданы все, чтобы отказ нельзя было списать на видимость.
# У директора есть и `unit.close` (D033, T076): закрывать часы точки вправе
# тот, кто ведёт месяц целиком, — иначе отпуск управляющего запирает период.
# У бухгалтера ФИКСТУРЫ его намеренно нет, и на этом стоит проверка, что право
# не выдаётся заодно с `period.approve` и не следует из «вижу все точки». У
# бухгалтера ПРОДУКТА оно есть с T115 (D036, доступ равен директорскому) —
# набор здесь сужен нарочно, как это делает `narrowed_ledgers` с регистрами:
# механизм базы обязан проверяться и после того, как роль его переросла.
P_DIRECTOR = (
    '["timesheet.edit", "payrun.calculate", "period.approve", "period.reopen",'
    ' "payslip.freeze", "retro.post", "unit.close"]'
)
P_ACCOUNTANT = (
    '["timesheet.edit", "payrun.calculate", "period.approve", "payslip.freeze"]'
)
P_MANAGER = '["timesheet.edit", "unit.close"]'
P_ADMIN = '["directory.manage", "rules.manage", "roles.manage"]'


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
               -- Перевод из кассы в банк и пополнение кассы: событие, которое
               -- надо накапливать ради сверки наличных, но ни расход, ни
               -- выручка. В P&L такие статьи не попадают по `kind` (T107).
               (%s, null, 'cash_transfer', 'Перевод наличных',  'transfer', 95)""",
        (I_REVENUE, I_FOOD, I_UTILITIES, I_LABOUR, I_TAXES, I_TOTAL, I_TRANSFER),
    )
    conn.execute(
        """insert into counterparties (id, tenant_id, title)
           values (%s, %s, 'EPS Elektro'), (%s, %s, 'Metro')""",
        (CP_EPS, T1, CP_METRO, T1),
    )
    conn.execute(
        """insert into roles (id, tenant_id, code, title, visible_ledgers, permissions) values
               (%s, %s,   'director',   'Оперативный директор',
                   '{official,supplementary,internal}', %s),
               (%s, %s,   'accountant', 'Бухгалтер',
                   '{official,supplementary,internal}', %s),
               (%s, %s,   'manager',    'Управляющий точки',    '{official,supplementary}', %s),
               (%s, %s,   'admin',      'Администратор сети',
                   '{official,supplementary,internal}', %s),
               (%s, null, 'support',    'Поддержка сервиса',    '{official}', '[]'),
               (%s, %s,   'director',   'Директор партнёра',
                   '{official,supplementary,internal}', %s)""",
        (
            R_DIRECTOR, T1, P_DIRECTOR,
            R_ACCOUNTANT, T1, P_ACCOUNTANT,
            R_MANAGER, T1, P_MANAGER,
            R_ADMIN, T1, P_ADMIN,
            R_SYSTEM,
            R_OTHER, T2, P_DIRECTOR,
        ),
    )
    conn.execute(
        """insert into memberships (tenant_id, user_id, role_id, unit_ids) values
               (%s, %s, %s, null),
               (%s, %s, %s, null),
               (%s, %s, %s, array[%s]::uuid[]),
               (%s, %s, %s, null),
               (%s, %s, %s, null)""",
        (
            T1, USER_DIRECTOR, R_DIRECTOR,
            T1, USER_ACCOUNTANT, R_ACCOUNTANT,
            T1, USER_MANAGER, R_MANAGER, U_NS1,
            T1, USER_ADMIN, R_ADMIN,
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

        # Django уже настроен на этот самый адрес — при импорте `conftest`, а не
        # здесь (см. `_configure_django`). Проверяем, а не предполагаем: если
        # имя базы разъедется, веб-тесты пошли бы в чужую базу и рассказали бы
        # об этом не «настройка разъехалась», а красными числами приёмки.
        assert dsn == WEB_DSN, f"Django настроен на {WEB_DSN}, а база заведена {dsn}"

        from django.test.utils import setup_test_environment, teardown_test_environment

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


@pytest.fixture
def period_restored(web_env):
    """Снимок табеля июня до теста и точный возврат к нему после.

    **Обязательна каждому тесту, который пишет в табель общей базы** — с экрана,
    импортом или напрямую. База `web_env` живёт весь прогон и одна на все
    модули, а суммы расчёта считаются из этого самого табеля: строка, оставленная
    изменённой, двигает контрольные числа у всех, кто считает период после.

    Почему это не «гигиена на будущее», а починка. Экранные тесты сетки
    (`test_timesheets.py`) писали в первую строку табеля и не возвращали её.
    Прогон целиком оставался зелёным только потому, что `test_timesheet_import.py`
    сортируется по имени **раньше** `test_timesheets.py` и успевал посчитать
    суммы до порчи. Стоило запустить эти два файла в другом порядке — и
    `test_import_does_not_move_the_calculation` краснел на числе 1 951 806,13,
    хотя импорт был ни при чём. Красный не по своей вине хуже отсутствующего
    теста: настоящую поломку в следующий раз спишут на тот же шум.

    Дни возвращаются вместе со строками: итог обязан сходиться с ними, иначе
    инвариант подневного хранения рвётся молча.
    """
    from core.models import Timesheet, TimesheetDay

    fields = [f.name for f in Timesheet._meta.concrete_fields]
    sheets = [
        {name: getattr(row, name) for name in fields}
        for row in Timesheet.objects.filter(period=JUNE)
    ]
    day_fields = [f.name for f in TimesheetDay._meta.concrete_fields]
    days = [
        {name: getattr(day, name) for name in day_fields}
        for day in TimesheetDay.objects.filter(timesheet__period=JUNE)
    ]
    yield
    TimesheetDay.objects.filter(timesheet__period=JUNE).delete()
    Timesheet.objects.filter(period=JUNE).delete()
    Timesheet.objects.bulk_create([Timesheet(**row) for row in sheets])
    TimesheetDay.objects.bulk_create([TimesheetDay(**day) for day in days])


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
        # Утверждённый расчёт база не даёт ни менять, ни удалять (T023), и это
        # правило действует в том числе на суперпользователя — держит триггер, а
        # не политика. Уборка поэтому сначала открывает период заново: иначе
        # фикстура падала бы на расчёте, утверждённом соседним тестом, вместо
        # того чтобы убрать за ним.
        # Откат без причины база не пропускает (T025) — и это правило тоже
        # держит триггер, то есть действует на суперпользователя. Причина
        # выставляется в той же транзакции: при autocommit каждый оператор был
        # бы своей, и настройка не дожила бы до `update`.
        with conn.transaction():
            # По одному и **от позднего месяца к раннему**: разница, уехавшая
            # задним числом (T026), не даёт открыть месяц-источник, пока лежит
            # в утверждённом получателе. Одним оператором порядок строк не
            # задан, и уборка падала бы через раз (та же ловушка, что у сида).
            # Причина ставится на каждый откат заново: триггер журнала гасит её
            # после записи, чтобы пересчёт не наследовал чужое объяснение.
            for (payrun_id,) in conn.execute(
                f"select id from payruns where tenant_id in {tenants} "
                f"and status = 'approved' order by period desc"
            ).fetchall():
                conn.execute(
                    "select set_config('app.transition_reason', %s, true)",
                    ("уборка тестовых данных",),
                )
                conn.execute(
                    "update payruns set status = 'reopened' where id = %s", (payrun_id,)
                )
        # Замороженную строку ведомости база не даёт ни менять, ни удалять
        # (T027) — держит триггер, то есть и на суперпользователя тоже. Уборка
        # снимает заморозки, а не стирает их: снятие разрешено в любом
        # состоянии периода как раз ради обслуживания.
        conn.execute(
            f"update payslip_freezes set released_at = now() "
            f"where tenant_id in {tenants} and released_at is null"
        )
        # Задания фонового расчёта (T024) стоят рядом с расчётами и на `payruns`
        # не ссылаются, поэтому вместе с ними не уходят. Незавершённое задание,
        # оставшееся от соседнего теста, не даёт завести новое — этого требует
        # частичный уникальный индекс `payrun_jobs_active_uniq`.
        conn.execute(f"delete from payrun_jobs where tenant_id in {tenants}")
        # Переносы разницы задним числом (T026) на `payruns` не ссылаются —
        # они вход, а не результат, — поэтому вместе с расчётами не уходят.
        # Удалять их можно: период-получатель выше уже открыт заново, а запрет
        # стоит только на утверждённой разнице.
        conn.execute(f"delete from retro_adjustments where tenant_id in {tenants}")
        conn.execute(f"delete from pay_components where tenant_id in {tenants}")
        conn.execute(f"delete from payslip_totals where tenant_id in {tenants}")
        conn.execute(f"delete from payslips where tenant_id in {tenants}")
        conn.execute(f"delete from payruns where tenant_id in {tenants}")


@contextmanager
def narrowed_ledgers(dsn: str, code: str, ledgers: list[str]):
    """Временно сузить набор регистров роли в базе — и вернуть его обратно.

    **Зачем это понадобилось.** До D036 в продукте была роль, у которой право
    считать период есть, а набор регистров неполный (бухгалтер), — и на ней
    держались проверки того, что база не даёт записать строку скрытого от роли
    регистра. После D036 такой роли в сиде нет: у бухгалтера и директора набор
    полон, а у управляющего неполон, но права `payrun.calculate` у него нет —
    его отказ пришёл бы от прав, а не от регистров, и проверка стала бы зелёной
    не по своей причине.

    Удалить проверки было нельзя: механизм видимости остаётся на месте и будет
    сужаться дальше, «там где надо» (D036). Поэтому условие создаётся явно —
    роли на время теста оставляют неполный набор, ровно как это сделает партнёр
    через экран ролей.

    Правка идёт владельцем схемы (политики на него не действуют) и всегда
    откатывается: набор регистров роли — общее состояние базы, оставленный
    сузённым он молча испортил бы числа соседним тестам.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "select visible_ledgers from roles where code = %s and tenant_id is not null",
            (code,),
        ).fetchone()
        assert row is not None, f"роли {code} нет в базе — сужать нечего"
        # Тип `ledger` на этом соединении не зарегистрирован (регистрирует его
        # драйвер приложения, `core/db_types.py`), поэтому массив приезжает
        # строкой `{official,...}`. Разбираем явно: `list()` от строки дал бы
        # список букв, и восстановление молча положило бы мусор.
        before = (
            row[0] if isinstance(row[0], list)
            else [part for part in row[0].strip("{}").split(",") if part]
        )
        conn.execute(
            # Через text[]: список строк уезжает в базу неизвестным типом, и
            # прямой каст к `ledger[]` Postgres понимает как каст к скаляру.
            "update roles set visible_ledgers = %s::text[]::ledger[]"
            " where code = %s and tenant_id is not null",
            (list(ledgers), code),
        )
    try:
        yield
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "update roles set visible_ledgers = %s::text[]::ledger[]"
                " where code = %s and tenant_id is not null",
                (before, code),
            )


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


@contextmanager
def as_directory_admin(conn):
    """Подготовить справочник — временно администратором сети (T018).

    С миграции `0130` завести человека, группу или точку вправе только тот, у
    кого есть `directory.manage`, а это в фикстуре один администратор сети.
    Тестам ниже человек нужен как материал: они проверяют заморозку расчёта или
    пересечение правил, а не право вести справочник. Подменяется только
    контекст пользователя — роль остаётся `app_user`, то есть политики
    продолжают действовать и подготовка идёт настоящим путём продукта, а не
    обходом RLS владельцем схемы.
    """
    previous = conn.execute("select current_setting('app.user_id', true)").fetchone()[0]
    conn.execute("select set_config('app.user_id', %s, true)", (USER_ADMIN,))
    try:
        yield conn
    finally:
        conn.execute("select set_config('app.user_id', %s, true)", (previous or "",))


def pay_component(conn, *, ledger: str, amount: str = "1000.00", code: str = "hours.regular",
                  tenant: str = T1) -> str:
    """Компонент выплаты нужного регистра — материал для проверки видимости."""
    with as_directory_admin(conn):
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
