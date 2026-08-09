"""
Изоляция демо: запись в демо не попадает в боевую базу (D016).

Это главная проверка блока. Демо сбрасывается пересозданием базы целиком, а
рядом живёт база с ФИО и суммами живых людей — цена ошибки здесь не «неверные
данные на экране», а стёртый месяц партнёра.

Проверяется не намерение, а поведение: рядом поднимаются **две настоящие базы**,
одна с данными продукта, другая пустая под демо, и на них гоняются те же
команды, что запустит человек.
"""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager

import pytest

from conftest import MANAGE_PY, run_manage, temp_database

# Что считаем, чтобы утверждать «база не изменилась». Не одна таблица: запись
# могла бы приехать в любую, а «сотрудников столько же» ничего не говорит о
# ведомостях.
COUNTED = (
    "tenants", "legal_entities", "units", "employees", "employment_terms",
    "timesheets", "payruns", "payslips", "pay_components", "roles",
    "memberships", "rule_overrides", "periods",
)


def manage(dsn: str, *args: str, demo: str | None = None) -> subprocess.CompletedProcess:
    """Команда Django на базе `dsn`, с заданным адресом демо-базы.

    Отдельно от `conftest.run_manage`, потому что здесь нужен и провал: половина
    проверок этого файла — про то, что команда **отказалась** работать.
    """
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "SECRET_KEY": "test-only-not-a-secret",
        "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    if demo is None:
        env.pop("DEMO_DATABASE_URL", None)
    else:
        env["DEMO_DATABASE_URL"] = demo
    return subprocess.run(
        [sys.executable, str(MANAGE_PY), *args],
        env=env, capture_output=True, text=True,
    )


def snapshot(dsn: str) -> dict[str, int]:
    psycopg = pytest.importorskip("psycopg")

    with psycopg.connect(dsn) as conn:
        return {
            table: conn.execute(f"select count(*) from {table}").fetchone()[0]
            for table in COUNTED
        }


def tenant_codes(dsn: str) -> set[str]:
    psycopg = pytest.importorskip("psycopg")

    with psycopg.connect(dsn) as conn:
        return {row[0] for row in conn.execute("select code from tenants").fetchall()}


@contextmanager
def two_databases():
    """Боевая база с данными продукта и пустая база под демо — рядом."""
    with temp_database("live") as live:
        run_manage(live, "seed_dev")
        with temp_database("demo") as demo:
            yield live, demo


@pytest.fixture(scope="module")
def databases():
    with two_databases() as pair:
        yield pair


# --- предохранитель по адресу --------------------------------------------------


def test_seed_demo_refuses_to_run_on_the_live_database(databases):
    """Команда, запущенная в экземпляре продукта, не пишет ничего.

    Именно этот случай ловит сравнение адресов: `DATABASE_URL` показывает на
    боевую базу, `DEMO_DATABASE_URL` — на демо, а `seed_demo` запущен не там.
    """
    live, demo = databases
    before = snapshot(live)

    result = manage(live, "seed_demo", demo=demo)

    assert result.returncode != 0, result.stdout
    assert "не на демо-базе" in result.stderr
    assert snapshot(live) == before, "боевая база изменилась"
    assert "demo" not in tenant_codes(live)


def test_seed_demo_refuses_without_the_demo_address(databases):
    """Пустой `DEMO_DATABASE_URL` — отказ, а не умолчание.

    Умолчание здесь означало бы «забыли переменную» → «наполнили что попало».
    """
    live, _demo = databases
    before = snapshot(live)

    result = manage(live, "seed_demo", demo=None)

    assert result.returncode != 0
    assert "DEMO_DATABASE_URL" in result.stderr
    assert snapshot(live) == before


# --- предохранитель по данным --------------------------------------------------


def test_seed_demo_refuses_a_database_that_holds_another_partner(databases):
    """Обе переменные показывают на боевую базу — и это ловится по данным.

    Сравнение адресов здесь бессильно и не могло бы помочь: у демо-экземпляра
    `DATABASE_URL` и `DEMO_DATABASE_URL` равны по замыслу. Отличить демо-базу от
    боевой можно только заглянув в неё: в демо один партнёр, и он
    демонстрационный.
    """
    live, _demo = databases
    before = snapshot(live)

    result = manage(live, "seed_demo", demo=live)

    assert result.returncode != 0, result.stdout
    assert "данные, которых в демо быть не может" in result.stderr
    assert "rs-dev" in result.stderr, "в отказе не назван найденный партнёр"
    assert snapshot(live) == before, "боевая база изменилась"


# --- собственно изоляция -------------------------------------------------------


def test_writing_into_demo_leaves_the_live_database_untouched(databases):
    """Наполнение демо не трогает боевую базу ни одной строкой."""
    live, demo = databases
    before = snapshot(live)

    result = manage(demo, "seed_demo", demo=demo)
    assert result.returncode == 0, result.stderr

    assert snapshot(live) == before, "боевая база изменилась после наполнения демо"
    assert tenant_codes(live) == {"rs-dev"}
    assert tenant_codes(demo) == {"demo"}


def test_demo_data_holds_nobody_from_the_development_seed(databases):
    """В демо-базе нет ни одного человека сида разработки — и наоборот.

    Проверка на пересечение, а не на количество: одинаковые имена в двух базах
    означали бы, что демо всё-таки наполнено чужими данными.
    """
    live, demo = databases
    psycopg = pytest.importorskip("psycopg")

    def people(dsn: str) -> set[str]:
        with psycopg.connect(dsn) as conn:
            return {
                row[0]
                for row in conn.execute("select external_id from employees").fetchall()
            }

    assert not (people(live) & people(demo))
