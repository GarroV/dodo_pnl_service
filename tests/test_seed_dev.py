"""
Сид тестовых данных: `manage.py seed_dev` наполняет чистую базу так, что на ней
проходит расчёт зарплатным движком.

Настоящая таблица партнёра здесь не участвует и участвовать не может (D028):
данные берутся из обезличенной фикстуры `tests/fixtures/plata-sample.xlsx`.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import run_manage, temp_database
from payroll import Employee, Timesheet, d


@pytest.fixture(scope="module")
def seeded():
    """Чистая база + один прогон seed_dev. Ровно то, что делает разработчик."""
    with temp_database("seed") as dsn:
        run_manage(dsn, "seed_dev")
        yield dsn


@pytest.fixture
def conn(seeded):
    psycopg = pytest.importorskip("psycopg")

    from core.db_types import register_enum_types

    with psycopg.connect(seeded) as c:
        register_enum_types(c)
        yield c


def test_seed_creates_tenant_and_org(conn):
    tenant = conn.execute(
        "select id, country_code, base_currency from tenants where code = 'rs-dev'"
    ).fetchone()
    assert tenant is not None, "сид не создал тенанта"
    assert tenant[1] == "RS"

    assert conn.execute(
        "select count(*) from legal_entities where tenant_id = %s", (tenant[0],)
    ).fetchone()[0] >= 1
    units = conn.execute(
        "select code from units where tenant_id = %s order by code", (tenant[0],)
    ).fetchall()
    assert [u[0] for u in units] == ["BG1", "NS1", "NS2"]


def test_seed_creates_roles_with_different_ledgers(conn):
    """Три роли спеки: директор видит всё, бухгалтер — только официальный регистр."""
    roles = dict(
        conn.execute(
            "select code, visible_ledgers from roles where tenant_id is not null"
        ).fetchall()
    )
    assert set(roles["director"]) == {"official", "supplementary", "internal"}
    assert set(roles["accountant"]) == {"official"}
    assert "manager" in roles

    # У управляющего доступ ограничен одной точкой
    manager_units = conn.execute(
        """select m.unit_ids from memberships m
             join roles r on r.id = m.role_id where r.code = 'manager'"""
    ).fetchone()[0]
    assert manager_units is not None and len(manager_units) == 1


def test_seed_creates_open_period(conn):
    rows = conn.execute("select period, status from periods").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "open"


def test_seed_loads_all_employees_from_fixture(conn, sample_rows):
    from_fixture = {row.employee.ext_id for row in sample_rows}
    loaded = {
        row[0] for row in conn.execute("select external_id from employees").fetchall()
    }
    assert from_fixture <= loaded, "часть людей из фикстуры не доехала"
    assert len(loaded) >= 30

    # У каждого — действующие условия найма и часы за период
    count = len(loaded)
    assert conn.execute("select count(*) from employment_terms").fetchone()[0] == count
    assert conn.execute("select count(*) from timesheets").fetchone()[0] == count


def test_seed_covers_all_three_ledgers(conn):
    """Все три регистра представлены строками — иначе сид не показывает продукт.

    Обезличенная фикстура даёт только официальный и дополнительный: курьеров
    (внутренний регистр) в ней нет. Пока их не было в сиде, сценарий скрытия
    регистра приходилось проверять вставкой строки руками (T045).
    """
    ledgers = {
        row[0]
        for row in conn.execute(
            """select coalesce(t.ledger, g.ledger)
                 from employment_terms t join employee_groups g on g.id = t.group_id"""
        ).fetchall()
    }
    assert ledgers == {"official", "supplementary", "internal"}


def test_seed_does_not_work_around_the_courier_scheme(conn):
    """T053: курьеры считаются схемой своей группы, а не подставленной чужой.

    До этого сид переопределял им схему на условиях найма (`temporary`), потому
    что схемы `none` из пресета движок не знал (issue #45). Обход — это заглушка
    ради тестовых данных, и она обязана исчезнуть вместе с причиной.
    """
    rows = conn.execute(
        """select t.scheme, g.scheme
             from employment_terms t join employee_groups g on g.id = t.group_id
            where g.code = 'couriers'"""
    ).fetchall()
    assert rows, "в сиде нет ни одного курьера"
    for own, from_group in rows:
        assert own is None, f"курьеру подставлена схема {own} вместо групповой {from_group}"
        assert from_group == "direct"


def test_seed_loads_country_rules_into_the_table(conn):
    """Правила расчёта живут в базе (T011): без них расчёт периода не пойдёт."""
    presets = conn.execute(
        "select code, country_code from rule_presets where country_code = 'RS'"
    ).fetchall()
    assert [p[0] for p in presets] == ["serbia-2026"]


def test_seed_keeps_cash_payout_and_manual_correction(conn):
    """Значения, которые движок принимает, а схема раньше теряла (T051)."""
    with_cash = conn.execute(
        "select count(*) from timesheets where cash_payout > 0"
    ).fetchone()[0]
    assert with_cash >= 1, "в сиде нет ни одной выплаты наличными"

    corrections = conn.execute(
        """select manual_correction, correction_reason, corrected_by
             from timesheets where manual_correction is not null"""
    ).fetchall()
    assert corrections, "в сиде нет ни одной ручной корректировки"
    for amount, reason, author in corrections:
        assert amount is not None and reason and author, "правка без следа (D025)"


def test_engine_calculates_on_seeded_data(conn, engine):
    """Главное: на данных сида движок считает, а не падает и не выдаёт нули."""
    rows = conn.execute(
        """select e.external_id, g.code, coalesce(t.scheme, g.scheme),
                  t.base_rate, t.coefficient, coalesce(t.ledger, g.ledger),
                  ts.hours, ts.insured_hours, ts.norm_hours, ts.deduction
             from employees e
             join employment_terms t on t.employee_id = e.id
             join employee_groups g on g.id = t.group_id
             join timesheets ts on ts.employee_id = e.id"""
    ).fetchall()
    assert len(rows) >= 30

    for ext_id, group, scheme, rate, coeff, ledger, hours, insured, norm, deduction in rows:
        slip = engine.calculate(
            Employee(
                ext_id=ext_id, name=ext_id, group=group, scheme=scheme,
                base_rate=d(rate), coefficient=d(coeff), ledger=ledger,
            ),
            Timesheet(
                hours={k: d(v) for k, v in hours.items()},
                insured_hours=d(insured), norm_hours=d(norm), deduction=d(deduction),
            ),
        )
        assert slip.net > Decimal(0), f"нулевая выплата у {ext_id}"
        assert slip.total_cost >= slip.net
        assert slip.components


def test_enum_array_without_registration_is_a_string(seeded):
    """Обратная сторона: без регистрации типа значение действительно строка.

    Тест существует, чтобы положительная проверка не оказалась фиктивно
    зелёной — если однажды psycopg научится сам, здесь станет видно.
    """
    psycopg = pytest.importorskip("psycopg")

    with psycopg.connect(seeded) as raw:
        value = raw.execute("select visible_ledgers from roles limit 1").fetchone()[0]
    assert isinstance(value, str)


def test_orm_reads_enum_array_as_list(seeded):
    """Тот же путь через ORM: сигнал подключения должен срабатывать сам."""
    out = run_manage(
        seeded, "shell", "-c",
        "from core.models import Role;"
        "r = Role.objects.get(code='director');"
        "print(type(r.visible_ledgers).__name__, sorted(r.visible_ledgers))",
    ).stdout
    # shell печатает свою шапку про автоимпорт — интересна последняя строка
    assert out.strip().splitlines()[-1] == "list ['internal', 'official', 'supplementary']"


def test_seed_is_idempotent(seeded, conn):
    """Повторный прогон не удваивает данные: сид можно гонять сколько угодно."""
    before = conn.execute("select count(*) from employees").fetchone()[0]
    run_manage(seeded, "seed_dev")
    after = conn.execute("select count(*) from employees").fetchone()[0]
    assert after == before
