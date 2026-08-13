"""Отказ расчёта не рассказывает о тех, кого смотрящему не показывают (T114).

Восьмая утечка регистров и точек: страница месяца и опрос состояния расчёта
печатали `payrun_jobs.details` как есть. Это список `external_id` по **всему**
периоду — на боевых данных ЈМБГ, — и срез роли в него не заходил вовсе, потому
что данные едут не выборкой из таблиц, а полем задания, мимо политик базы.

Проверяется ровно то, чем утечка была видна человеку, и обе половины сразу:

1. управляющему точки не видно ни имён скрытого регистра, ни имён чужой точки —
   ни на странице, ни в ответе опроса;
2. директору по-прежнему видно, у кого именно не сошлось: отказ без этого
   бесполезен — чинить будет нечего;
3. счётчик задания (`done`/`total`) не рассказывает управляющему размер расчёта
   по всему партнёру: число строк чужого регистра — та же утечка, что имя
   (D014, D023);
4. в самом тексте отказа людей не пересчитывают: «35 чел.» — тот же счётчик,
   только словами.

Гоняется ролью `app_user`: контекст выставляет `DbContextMiddleware`, как в
продукте. Владельцем таблиц эта проверка была бы зелёной при снятых политиках.
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from conftest import body, login_as, period_url, wipe_payruns

JUNE = "2026-06-01"

# Внутренний регистр: управляющему NS1 эти строки не показывают нигде — ни в
# ведомости, ни в следе, ни в выгрузках.
HIDDEN_LEDGER = ("dev-courier-1", "dev-courier-2")
# Чужая точка: регистр управляющему виден, а точка BG1 — нет.
OTHER_UNIT = ("UROS ANDRIC",)
# Схема этого человека читает базу взносов — на нём проверяется отказ, в тексте
# которого людей пересчитывали.
STALE_BASE = "SANJA KOSTIC"


@pytest.fixture
def clean_payruns(web_env):
    wipe_payruns(web_env)
    return web_env


@contextmanager
def broken_scheme(dsn: str, external_ids: tuple[str, ...]):
    """Схема расчёта, которой нет в правилах страны, — и возврат обратно.

    Правится владельцем схемы: в живой системе то же самое делает администратор
    сети экраном справочника, и своих прав на это тесту не нужно. Возврат
    обязателен — набор схем общий для всех модулей прогона.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        before = conn.execute(
            "select t.id, t.scheme from employment_terms t"
            " join employees e on e.id = t.employee_id"
            " where e.external_id = any(%s)",
            (list(external_ids),),
        ).fetchall()
        assert before, f"в сиде нет сотрудников {external_ids} — ломать нечего"
        conn.execute(
            "update employment_terms set scheme = 'no_such_scheme' where id = any(%s)",
            ([row[0] for row in before],),
        )
    try:
        yield
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for term_id, scheme in before:
                conn.execute(
                    "update employment_terms set scheme = %s where id = %s",
                    (scheme, term_id),
                )


def failed_job(dsn: str) -> dict:
    """Что записано в задании на самом деле — эталон «что мы прячем»."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "select status::text, error, details from payrun_jobs"
            " order by created_at desc limit 1"
        ).fetchone()
    assert row is not None, "задания нет — расчёт не запускался"
    return {"status": row[0], "error": row[1], "details": list(row[2] or [])}


def refuse_calculation(client, dsn: str) -> str:
    """Директор считает период и получает отказ. Возвращает адрес периода."""
    login_as(client, "director")
    url = period_url(client)
    response = client.post(url + "calculate/", {"inline": "1"})
    assert response.status_code == 409, body(response)
    return url


def status_of(client, url: str) -> dict:
    response = client.get(url + "calculate/status/")
    assert response.status_code == 200
    return response.json()


# --- имена и ключи -----------------------------------------------------------


def test_the_page_does_not_name_a_hidden_ledger_to_the_unit_manager(client, clean_payruns):
    dsn = clean_payruns
    with broken_scheme(dsn, HIDDEN_LEDGER):
        url = refuse_calculation(client, dsn)
        # Сначала убеждаемся, что прятать есть что: имена лежат в задании.
        assert set(failed_job(dsn)["details"]) == set(HIDDEN_LEDGER)

        login_as(client, "manager")
        page = body(client.get(url))

    for name in HIDDEN_LEDGER:
        assert name not in page, f"на странице управляющего есть {name}"


def test_the_page_does_not_name_another_unit_to_the_unit_manager(client, clean_payruns):
    dsn = clean_payruns
    with broken_scheme(dsn, OTHER_UNIT):
        url = refuse_calculation(client, dsn)
        assert set(failed_job(dsn)["details"]) == set(OTHER_UNIT)

        login_as(client, "manager")
        page = body(client.get(url))

    for name in OTHER_UNIT:
        assert name not in page, f"на странице управляющего есть {name}"


def test_the_page_still_names_them_to_the_director(client, clean_payruns):
    """Обратная половина: отказ обязан оставаться полезным тому, кому он отдан."""
    dsn = clean_payruns
    with broken_scheme(dsn, HIDDEN_LEDGER):
        url = refuse_calculation(client, dsn)
        login_as(client, "director")
        page = body(client.get(url))

    for name in HIDDEN_LEDGER:
        assert name in page, f"директор не видит {name} — чинить будет нечего"


def test_the_status_route_answers_the_manager_without_names(client, clean_payruns):
    """Тот же список отдавался запросом, без страницы, — проверки там не было."""
    dsn = clean_payruns
    with broken_scheme(dsn, HIDDEN_LEDGER):
        url = refuse_calculation(client, dsn)

        login_as(client, "manager")
        state = status_of(client, url)

    assert state["details"] == []
    for name in HIDDEN_LEDGER:
        assert name not in state["error"]


def test_the_status_route_still_names_them_to_the_director(client, clean_payruns):
    dsn = clean_payruns
    with broken_scheme(dsn, HIDDEN_LEDGER):
        url = refuse_calculation(client, dsn)
        login_as(client, "director")
        state = status_of(client, url)

    assert set(state["details"]) == set(HIDDEN_LEDGER)


# --- счётчик -----------------------------------------------------------------


def test_the_counter_does_not_tell_the_manager_the_size_of_the_run(client, clean_payruns):
    """`total: 35` управляющему, у которого в табеле 17, — та же утечка, что имя."""
    dsn = clean_payruns
    tenant_id = str(
        psycopg.connect(dsn).execute("select id from tenants where code = 'rs-dev'").fetchone()[0]
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "insert into payrun_jobs (tenant_id, period, status, stage, done, total)"
            " values (%s, %s, 'running'::payrun_job_status, 'Считаем', 20, 35)",
            (tenant_id, JUNE),
        )

    login_as(client, "director")
    url = period_url(client)
    director = status_of(client, url)
    login_as(client, "manager")
    manager = status_of(client, url)

    assert (director["done"], director["total"]) == (20, 35)
    assert (manager["done"], manager["total"]) == (0, 0)
    # Полоса при этом живёт: доля — не число людей, и прятать её незачем.
    assert manager["percent"] == director["percent"] > 0


# --- счёт людей словами ------------------------------------------------------


def test_the_refusal_counts_nobody_in_its_own_text(client, clean_payruns, period_restored):
    """«35 чел.» внутри текста — тот же счётчик скрытых строк, только словами.

    Число людей и так стоит рядом списком имён — у того, кому список отдан.
    Поэтому оно убрано из самого сообщения, а не переписывается под смотрящего:
    сообщение одно на всех, и подгонять его под роль значило бы завести второй
    текст того же отказа.
    """
    dsn = clean_payruns
    with psycopg.connect(dsn, autocommit=True) as conn:
        broken = conn.execute(
            "update timesheets s set insured_hours = s.insured_hours + 1"
            " from employees e"
            " where e.id = s.employee_id and s.period = %s and e.external_id = %s"
            " returning s.id",
            (JUNE, STALE_BASE),
        ).fetchall()
    assert broken, f"в табеле июня нет строки {STALE_BASE} — ломать нечего"

    refuse_calculation(client, dsn)
    job = failed_job(dsn)

    assert "база для взносов" in job["error"]
    assert "чел." not in job["error"], job["error"]
    assert job["details"] == [STALE_BASE]


# --- совет вместо молчания ---------------------------------------------------


def test_the_manager_is_told_why_there_are_no_names(client, clean_payruns):
    """Исчезнувшее без объяснения читается как поломка продукта, а не запрет.

    Текст — свойство роли, а не данных: он стоит на месте подробностей всегда,
    когда роли не отдан весь расчёт, независимо от того, есть ли скрытые имена.
    Иначе его появление само рассказывало бы о них.
    """
    dsn = clean_payruns
    with broken_scheme(dsn, HIDDEN_LEDGER):
        url = refuse_calculation(client, dsn)
        login_as(client, "manager")
        page = body(client.get(url))

    assert "весь расчёт" in page
    # И причина отказа управляющему по-прежнему сказана: он должен знать, что
    # расчёт не выполнен и что с этим делать.
    assert "нет схем расчёта" in page
