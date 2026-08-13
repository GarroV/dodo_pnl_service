"""Бухгалтер откатывает утверждение и закрывает часы точки (T115, D036).

**Чего не хватало.** У роли `accountant` в сиде не было `period.reopen` и
`unit.close`: страница отвечала «Откат периода не входит в права вашей роли»,
а `POST /periods/<id>/reopen/` — `403`. Это противоречило и спеке дословно
(«Как бухгалтер, хочу откатить утверждение с указанием причины, чтобы опечатка
не превращалась в разбирательство»), и D036 («бухгалтер и оперативный директор
имеют РАВНЫЙ доступ»).

Механизм при этом был цел: тот же откат директором проходит, причина
обязательна, автор попадает в историю. Не хватало права у роли — то есть
данных, а не кода. Отсюда две проверки и одна отдельная:

1. **страницей**, ровно тем путём, которым пойдёт человек: посчитать →
   утвердить → откатить с причиной; и то же самое для часов точки;
2. **набором прав сида**: у бухгалтера и директора он равный, как сказано в
   D036. Проверяется сравнением двух ролей, а не списком строк: список пришлось
   бы держать в двух местах, и он разошёлся бы с сидом молча;
3. **миграцией на живой базе**: в базе, которая уже работает, сид не гоняется,
   и без миграции право появилось бы только у тех, кто пересоздаёт данные.
   Проверяется на отдельной базе, накатанной **до** этой миграции, с ролью в
   том виде, в каком она лежит у уже работающего партнёра.

Управляющего точки решение не касается (D031, D033): у него своя точка и два
регистра, и здесь ему ничего не добавляется.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from conftest import ADMIN_DSN, body, login_as, period_url, run_manage, wipe_payruns

JUNE = "2026-06-01"
REASON = "опечатка в часах"
# Роль до T115: ровно то, что лежит у партнёра, которому сид больше не гоняют.
BEFORE = '["timesheet.edit", "payrun.calculate", "period.approve", "payslip.freeze", "retro.post"]'


@pytest.fixture
def clean_payruns(web_env):
    wipe_payruns(web_env)
    yield web_env
    wipe_payruns(web_env)


def payrun_status(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        return conn.execute("select status from payruns").fetchone()[0]


# --- страницей, как это делает человек ---------------------------------------


def test_the_accountant_reopens_what_she_approved(client, clean_payruns):
    """Спека, must: «хочу откатить утверждение с указанием причины»."""
    dsn = clean_payruns
    login_as(client, "accountant")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    client.post(url + "approve/", follow=True)
    assert payrun_status(dsn) == "approved"

    response = client.post(url + "reopen/", {"reason": REASON}, follow=True)

    assert response.status_code == 200
    assert payrun_status(dsn) == "reopened"
    page = body(response)
    assert REASON in page
    # Автор — тот, кто нажал: в сиде имя учётки равно названию роли.
    assert "Бухгалтер" in page


def test_the_accountant_is_offered_the_reopen_button(client, clean_payruns):
    """Кнопка на месте, а не «адрес работает, но предлагать некому» (T072)."""
    login_as(client, "accountant")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    client.post(url + "approve/", follow=True)

    page = body(client.get(url))

    assert "reopen/" in page
    assert "Откат периода не входит в права" not in page


def test_the_accountant_closes_the_hours_of_a_unit(client, clean_payruns):
    """Вторая половина D036: `unit.close`, как у оперативного директора (D033)."""
    from core.models import Unit

    login_as(client, "accountant")
    grid = _grid_url(client)
    page = body(client.get(grid))
    assert 'name="unit"' in page, "бухгалтеру не предложена форма закрытия точки"

    unit = Unit.objects.filter(tenant__code="rs-dev").order_by("code").first()
    response = client.post(f"{grid}close/", {"unit": str(unit.id)}, follow=True)

    assert response.status_code == 200
    assert "не входит в права вашей роли" not in body(response)
    with psycopg.connect(clean_payruns) as conn:
        closed = conn.execute(
            "select count(*) from timesheet_closures"
            " where unit_id = %s and period = %s and reopened_at is null",
            (unit.id, JUNE),
        ).fetchone()[0]
    assert closed == 1

    # Прибираем за собой: закрытая точка не пускает правку часов, и соседние
    # модули получили бы отказ на пустом месте.
    client.post(f"{grid}reopen/", {"unit": str(unit.id)}, follow=True)


def _grid_url(client) -> str:
    import re

    html = body(client.get(period_url(client)))
    match = re.search(r'href="(/timesheets/[0-9a-f-]+/)"', html)
    assert match, "на странице периода нет ссылки на табель"
    return match.group(1)


# --- набор прав сида ----------------------------------------------------------


def test_the_seed_gives_the_accountant_the_same_rights_as_the_director(web_env):
    """D036 дословно: «бухгалтер и оперативный директор имеют РАВНЫЙ доступ».

    Сравнением ролей, а не сверкой со списком в тесте: список прав в двух местах
    разъедется с сидом молча — и молча же перестанет означать «равный».
    """
    with psycopg.connect(web_env) as conn:
        rights = dict(
            conn.execute(
                "select code, permissions from roles"
                " where code in ('director', 'accountant') and tenant_id is not null"
            ).fetchall()
        )
    assert set(rights["accountant"]) == set(rights["director"]), (
        "доступ бухгалтера разошёлся с директорским — это и есть D036"
    )


# --- миграция на базе, которая уже живёт --------------------------------------


def test_the_migration_opens_the_rights_where_the_seed_is_never_run():
    """Сид на живой базе не гоняется — право обязано приехать миграцией.

    База накатывается **до** этой миграции, роль кладётся в том виде, в каком
    она лежит у работающего партнёра, и только потом накатывается остаток.
    Иначе проверялась бы не миграция, а сид: он роли пересоздаёт.
    """
    pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"нет доступного Postgres по {ADMIN_DSN}: {exc}")

    dbname = f"dodo_pnl_test_accountant_{os.getpid()}"
    with admin:
        admin.execute(f'drop database if exists "{dbname}"')
        admin.execute(f'create database "{dbname}"')
    dsn = make_conninfo(ADMIN_DSN, dbname=dbname)

    try:
        run_manage(dsn, "migrate", "core", "0220_accountant_sees_every_ledger", "--no-input")
        with psycopg.connect(dsn, autocommit=True) as conn:
            tenant = conn.execute(
                "insert into tenants "
                "(code, title, country_code, base_currency, report_currency) "
                "values ('rs', 'Партнёр', 'RS', 'RSD', 'EUR') returning id"
            ).fetchone()[0]
            conn.execute(
                "insert into roles (tenant_id, code, title, visible_ledgers, permissions)"
                " values (%s, 'accountant', 'Бухгалтер',"
                "         array['official','supplementary','internal']::ledger[], %s::jsonb)",
                (tenant, BEFORE),
            )
            before = conn.execute(
                "select permissions from roles where code = 'accountant'"
            ).fetchone()[0]
            assert "period.reopen" not in before, "проверять нечего: право уже стоит"

        run_manage(dsn, "migrate", "--no-input")

        with psycopg.connect(dsn, autocommit=True) as conn:
            after = conn.execute(
                "select permissions from roles where code = 'accountant'"
            ).fetchone()[0]
        assert "period.reopen" in after, (
            "миграция не открыла откат периода — на живой базе бухгалтер "
            "останется без него, потому что сид там не гоняется"
        )
        assert "unit.close" in after
        # Ничего не потеряно по дороге: право добавляется, а не переписывает набор.
        assert set(BEFORE.strip("[]").replace('"', "").split(", ")) <= set(after)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as cleanup:
            cleanup.execute(f'drop database if exists "{dbname}" with (force)')
