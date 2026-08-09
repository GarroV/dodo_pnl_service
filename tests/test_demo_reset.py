"""
Сброс демо: пересоздание базы из эталона возвращает стенд к исходному виду.

Проверяется то, ради чего сброс вообще нужен: посетитель что-то поменял, прошло
время — и демо снова такое же, каким его показывали заказчику. Без ручных
действий: команду выполняет служба по расписанию, а не человек кнопкой.

Базы здесь настоящие и удаляются за собой. Имя демо-базы уникально для процесса:
параллельные прогоны не должны пересоздавать базу друг другу.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from conftest import ADMIN_DSN, MANAGE_PY

DEMO_MARKER = "dodo-pnl-demo"


def dsn_for(dbname: str) -> str:
    from psycopg.conninfo import make_conninfo

    return make_conninfo(ADMIN_DSN, dbname=dbname)


def manage(dsn: str, *args: str, demo: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "DEMO_DATABASE_URL": demo,
        "SECRET_KEY": "test-only-not-a-secret",
        "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    return subprocess.run(
        [sys.executable, str(MANAGE_PY), *args],
        env=env, capture_output=True, text=True,
    )


def drop(dbname: str) -> None:
    import psycopg

    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{dbname}" with (force)')


@pytest.fixture(scope="module")
def demo_stand():
    """Демо-база и её эталон, собранные так же, как их собирает служба.

    Первый сброс на пустом месте обязан дать работающий стенд сам — эталона ещё
    нет, и команда собирает его. Это тоже часть проверки: «сброс без ручных
    действий» означает в том числе первый запуск.
    """
    psycopg = pytest.importorskip("psycopg", reason="нужен psycopg: pip install -e '.[dev]'")
    try:
        psycopg.connect(ADMIN_DSN, autocommit=True).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"нет доступного Postgres по {ADMIN_DSN}: {exc}")

    dbname = f"dodo_pnl_demo_test_{os.getpid()}"
    template = f"{dbname}_template"
    demo = dsn_for(dbname)
    drop(dbname)
    drop(template)
    try:
        result = manage(demo, "demo_reset", demo=demo)
        assert result.returncode == 0, result.stderr
        yield demo, dbname, template
    finally:
        drop(dbname)
        drop(template)


def rows(dsn: str, sql: str):
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute(sql).fetchall()


def count(dsn: str, table: str) -> int:
    return rows(dsn, f"select count(*) from {table}")[0][0]


def test_first_reset_builds_a_working_stand(demo_stand):
    """Сброс на пустом месте даёт наполненное демо, а не отказ."""
    demo, _dbname, template = demo_stand

    assert count(demo, "employees") == 30
    assert count(demo, "periods") == 3
    approved = rows(demo, "select count(*) from payruns where status = 'approved'")
    assert approved[0][0] == 2, "закрытых периодов должно быть два"
    assert count(demo, "pay_components") > 0, "ведомости не посчитаны"

    # Эталон собран рядом и помечен: непомеченную базу сброс удалять не станет.
    marker = rows(dsn_for(template), "select marker from demo_stamp")
    assert marker == [(DEMO_MARKER,)]


def test_reset_returns_the_stand_to_the_reference_state(demo_stand):
    """Посетитель наследил — сброс всё вернул. Без единого ручного действия."""
    import psycopg

    demo, _dbname, _template = demo_stand
    before = count(demo, "employees")
    components_before = count(demo, "pay_components")

    with psycopg.connect(demo, autocommit=True) as conn:
        # Пишем от имени владельца схемы: посетитель ходит ролью приложения, но
        # проверяем мы не права, а то, что след любой записи исчезает.
        #
        # Портим **открытый** период: утверждённый расчёт база менять не даёт
        # никому, и это правильное поведение продукта, а не помеха проверке.
        conn.execute(
            """delete from pay_components where payslip_id in (
                   select p.id from payslips p
                     join payruns r on r.id = p.payrun_id
                    where r.status <> 'approved' limit 5)"""
        )
        conn.execute(
            "insert into employees (id, tenant_id, external_id, first_name, last_name)"
            " select gen_random_uuid(), id, 'stray', 'Stray', 'Visitor' from tenants"
        )
    assert count(demo, "employees") == before + 1
    assert count(demo, "pay_components") < components_before

    result = manage(demo, "demo_reset", demo=demo)
    assert result.returncode == 0, result.stderr

    assert count(demo, "employees") == before
    assert count(demo, "pay_components") == components_before
    assert not rows(demo, "select 1 from employees where external_id = 'stray'")


def test_reset_is_repeatable_and_deterministic(demo_stand):
    """Два сброса подряд дают одно и то же — включая идентификаторы.

    Идентификаторы важны отдельно: ссылку на конкретную ведомость показывают
    заказчику, и после ночного сброса она обязана открывать ту же страницу.
    """
    demo, _dbname, _template = demo_stand
    ids = rows(demo, "select id from payslips order by id")

    result = manage(demo, "demo_reset", demo=demo)
    assert result.returncode == 0, result.stderr

    assert rows(demo, "select id from payslips order by id") == ids


def test_reset_refuses_a_database_it_did_not_create(demo_stand):
    """Непомеченную базу сброс не удаляет, чем бы ни было заполнено окружение.

    Это последний предохранитель: он работает даже тогда, когда
    `DEMO_DATABASE_URL` показывает на чужую живую базу.
    """
    import psycopg

    demo, _dbname, _template = demo_stand
    stranger = f"dodo_pnl_stranger_{os.getpid()}"
    drop(stranger)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'create database "{stranger}"')
    try:
        with psycopg.connect(dsn_for(stranger), autocommit=True) as conn:
            conn.execute("create table important (id int)")
            conn.execute("insert into important values (1)")

        result = manage(dsn_for(stranger), "demo_reset", demo=dsn_for(stranger))

        assert result.returncode != 0, result.stdout
        assert "не помечена как демо" in result.stderr
        assert rows(dsn_for(stranger), "select id from important") == [(1,)]
    finally:
        drop(stranger)
        drop(f"{stranger}_template")
