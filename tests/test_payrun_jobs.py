"""Расчёт периода фоновой задачей: контекст, прогресс, идемпотентность (T024).

Что здесь проверяется и почему именно это:

1. **Фоновая задача сама представляется базе.** Это назван­ный в `plan.md` риск:
   у задачи нет HTTP-запроса, значит, контекст пользователя выставить некому, и
   без него она пойдёт ролью подключения — а в разработке это владелец базы,
   которого политики не ограничивают. Поэтому тестов два: «без контекста не
   видно ничего» и «задача чужого тенанта не видит задания», второй краснеет
   ровно тогда, когда контекст из задачи убрали.
2. **Повторный запуск не плодит ведомости.** Держит база: частичный уникальный
   индекс на незавершённое задание и перевод `queued → running` условным
   `update`. Проверяются оба контура по отдельности — совпадение чисел на
   выходе не доказывает, что сработал тот, который мы имели в виду.
3. **Прогресс виден снаружи, пока расчёт идёт.** То есть пишется по другому
   соединению. Проверка честная: пока на основном соединении открыта
   незавершённая транзакция, третье соединение обязано видеть отметку.
4. **Заморозка утверждённого периода фону не уступает** (T023), и права с
   регистрами перечитываются в момент исполнения, а не берутся из очереди.

Тесты гоняются на живом Postgres с сидом (фикстура `web_env`); без Postgres
пропускаются вместе с остальными тестами схемы.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest

from conftest import body, login_as, period_url, wipe_payruns

JUNE = "2026-06-01"


@pytest.fixture
def clean_payruns(web_env):
    """Известное состояние: ни расчётов, ни заданий. Модули делят одну базу."""
    wipe_payruns(web_env)
    return web_env


# --- помощники ---------------------------------------------------------------


def raw(dsn: str, sql: str, params=()) -> list[tuple]:
    """Прямой запрос владельцем базы: эталон «что там на самом деле»."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, params).fetchall()


def tenant_and_users(dsn: str) -> tuple[str, dict[str, str]]:
    tenant = raw(dsn, "select id from tenants where code = 'rs-dev'")[0][0]
    users = dict(
        raw(
            dsn,
            "select username, id from users where username in ('director','accountant','manager')",
        )
    )
    return str(tenant), {k: str(v) for k, v in users.items()}


def payslip_count(dsn: str) -> int:
    return raw(dsn, "select count(*) from payslips")[0][0]


def job_row(dsn: str, job_id) -> dict:
    row = raw(
        dsn,
        "select status::text, stage, done, total, error, background from payrun_jobs where id = %s",
        (str(job_id),),
    )
    assert row, "задания нет в базе"
    keys = ("status", "stage", "done", "total", "error", "background")
    return dict(zip(keys, row[0], strict=True))


def make_job(dsn: str, *, tenant_id: str, actor_id: str | None, status: str = "queued",
             created_shift: int = 0) -> str:
    """Задание в нужном состоянии, мимо приложения — материал для проверок."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                """insert into payrun_jobs
                       (tenant_id, period, status, requested_by, created_at)
                   values (%s, %s, %s::payrun_job_status, %s, now() - make_interval(secs => %s))
                   returning id""",
                (tenant_id, JUNE, status, actor_id, created_shift),
            ).fetchone()[0]
        )


# --- контекст тенанта в фоновой задаче ---------------------------------------


def test_without_a_context_nothing_is_visible(clean_payruns):
    """Основание всей задачи: контекст не выставлен — данных нет вовсе.

    Это не проверка Django, а проверка того, что фону есть что терять: если бы
    без контекста было видно всё, забытый `db_context` не проявился бы никак.
    """
    from core.models import Tenant, Timesheet
    from web.dbcontext import db_context

    with db_context(None):
        assert Tenant.objects.count() == 0
        assert Timesheet.objects.count() == 0


def test_the_background_task_sets_its_own_context(clean_payruns):
    """Задача считает период без единого HTTP-запроса — и видит данные."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    jobs.run_job(job_id, users["director"])

    assert job_row(dsn, job_id)["status"] == "done"
    assert payslip_count(dsn) > 0
    assert raw(dsn, "select status::text from payruns")[0][0] == "calculated"


def test_a_stranger_context_does_not_reach_the_job(clean_payruns):
    """Задание чужого тенанта под чужим контекстом не видно — и не считается.

    Краснеет ровно тогда, когда из задачи убрали `db_context`: без него
    соединение остаётся владельцем базы, и чужой тенант становится виден.
    """
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    stranger = "d1111111-0000-0000-0000-0000000000ff"
    with pytest.raises(jobs.JobUnavailable):
        jobs.run_job(job_id, stranger)

    assert payslip_count(dsn) == 0
    assert job_row(dsn, job_id)["status"] == "queued"


def test_the_jobs_of_another_tenant_are_invisible_to_the_database_itself(clean_payruns):
    """Изоляцию заданий держит база, а не фильтр в выборке.

    Фильтр по тенанту в `last_job` можно убрать — и ни один тест выше не
    покраснеет: чужое задание всё равно не покажется, потому что его прячет
    политика. Два контура прикрывают друг друга, и оттого ни один не проверен.
    Поэтому спрашивается сама база под ролью приложения: владелец таблиц и
    суперпользователь политики обходят, и на них проверка была бы фиктивной.
    """
    import psycopg

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    mine = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    with psycopg.connect(dsn, autocommit=True) as conn:
        stranger_tenant = conn.execute(
            """insert into tenants (code, title, country_code, base_currency)
               values ('zz-rls', 'Сосед', 'ZZ', 'ZZZ') returning id"""
        ).fetchone()[0]
        stranger_job = conn.execute(
            "insert into payrun_jobs (tenant_id, period, status) "
            "values (%s, %s, 'queued') returning id",
            (stranger_tenant, JUNE),
        ).fetchone()[0]

    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("set local role app_user")
            conn.execute(
                "select set_config('app.user_id', %s, true)", (users["director"],)
            )
            seen = {
                str(row[0])
                for row in conn.execute("select id from payrun_jobs").fetchall()
            }
            assert seen == {mine}, seen
            # И записать в чужой тенант нельзя: у политики есть `with check`.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "insert into payrun_jobs (tenant_id, period, status) "
                    "values (%s, %s, 'queued')",
                    (stranger_tenant, "2026-07-01"),
                )
            conn.rollback()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("delete from payrun_jobs where id = %s", (stranger_job,))
            conn.execute("delete from tenants where id = %s", (stranger_tenant,))


def test_an_internal_failure_reaches_the_queue_and_the_page(clean_payruns):
    """Поломка не выдаётся за выполненную задачу — ни человеку, ни очереди.

    Получателей два, и правду должны узнать оба. Человек — по заданию: там
    записан отказ его словами. Очередь — по исключению: `django-q` метит задачу
    упавшей, только если она бросила. Проглотить исключение и вернуться было
    можно, не сломав ни одного теста: очередь записала бы «выполнено» поверх
    поломки, а на странице при этом стояло бы «оборвался».
    """
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    def explode(*_args, **_kwargs):
        raise RuntimeError("сломалось внутри расчёта")

    original = jobs.run_calculation
    jobs.run_calculation = explode
    try:
        with pytest.raises(RuntimeError):
            jobs.run_job(job_id, users["director"])
    finally:
        jobs.run_calculation = original

    row = job_row(dsn, job_id)
    assert row["status"] == "failed"
    assert row["error"] == jobs.BROKEN
    assert payslip_count(dsn) == 0


# --- идемпотентность ---------------------------------------------------------


def test_the_calculation_runs_as_the_app_role_not_the_owner(clean_payruns):
    """Тот самый риск из `plan.md`, проверенный там, где он живёт.

    «Задача выставила контекст» и «расчёт идёт под контекстом» — разные
    утверждения. Убрать `db_context` из `run_calculation` можно было, не сломав
    ни одного теста: расчёт продолжал считаться — но уже владельцем таблиц,
    которого RLS не ограничивает вовсе, и заметить это по числам нельзя.
    Поэтому соединение расспрашивается изнутри самого расчёта.
    """
    from django.db import connection

    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    seen: list[tuple[str, str]] = []

    class ContextSpy:
        def say(self, stage, done=0, total=0):
            with connection.cursor() as cursor:
                cursor.execute(
                    "select current_user, coalesce(current_setting('app.user_id', true), '')"
                )
                seen.append(cursor.fetchone())

    jobs.run_calculation(job_id, users["director"], reporter=ContextSpy())

    assert seen, "расчёт не отчитался ни разу — спрашивать было не у кого"
    assert set(seen) == {("app_user", users["director"])}, seen


def test_a_second_active_job_cannot_be_created(clean_payruns):
    """Незавершённое задание на период ровно одно — и это правило базы."""
    import psycopg

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])
    with pytest.raises(psycopg.errors.UniqueViolation):
        make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])


def test_the_job_is_taken_by_one_runner_only(clean_payruns):
    """Второй заход по тому же заданию не делает ничего: перевод условный."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    jobs.run_job(job_id, users["director"])
    first = payslip_count(dsn)
    assert first > 0

    # Та же задача пришла второй раз — так бывает, когда очередь решила, что
    # рабочий процесс потерялся. Считать заново она не должна.
    jobs.run_job(job_id, users["director"])
    assert payslip_count(dsn) == first


def test_only_one_runner_wins_the_claim(clean_payruns):
    """Второй контур идемпотентности — сам по себе, а не через `run_job`.

    В `run_job` перед захватом стоит быстрая проверка статуса, и она одна
    закрывала оба теста выше: захват можно было сделать безусловным, ничего не
    покраснело бы. Но проверка статуса отдельным запросом — это гонка (между ней
    и записью успевает вклиниться второй исполнитель), и настоящая гарантия —
    условный `update`. Здесь оба исполнителя видят `queued` одновременно, как в
    настоящей гонке, и выиграть обязан ровно один.
    """
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    first = jobs.Reporter(job_id, users["director"], background=True)
    second = jobs.Reporter(job_id, users["director"], background=True)

    assert first.claim() is True
    assert second.claim() is False
    assert job_row(dsn, job_id)["status"] == "running"


def test_two_runs_in_a_row_do_not_double_the_sheet(clean_payruns):
    """Расчёт целиком дважды: ведомость та же, а не второй комплект."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)

    first_job = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])
    jobs.run_job(first_job, users["director"])
    first = payslip_count(dsn)

    second_job = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])
    jobs.run_job(second_job, users["director"])

    assert payslip_count(dsn) == first
    assert raw(dsn, "select count(*) from payrun_jobs where status = 'done'")[0][0] == 2


# --- прогресс ----------------------------------------------------------------


def test_progress_is_visible_before_the_calculation_commits(clean_payruns):
    """Смысл отдельного соединения: отметка видна, пока расчёт ещё не закончен.

    Порча, которая это ловит: писать прогресс по основному соединению. Тогда
    отметка окажется внутри незавершённой транзакции, и снаружи её не будет
    видно до самого конца — то есть ровно тогда, когда она уже не нужна.
    """
    from django.db import transaction

    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    from web.dbcontext import db_context

    with db_context(users["director"]):
        assert transaction.get_connection().in_atomic_block
        reporter = jobs.Reporter(job_id, users["director"], background=True)
        reporter.say("Считаем сотрудников", done=7, total=30)
        # Третье соединение: транзакция расчёта ещё открыта.
        seen = job_row(dsn, job_id)
        assert seen["stage"] == "Считаем сотрудников"
        assert (seen["done"], seen["total"]) == (7, 30)


def test_the_calculation_reports_every_stage(clean_payruns):
    """Этапы не выдуманы страницей: их называет сам расчёт."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])

    said: list[tuple[str, int, int]] = []

    class Spy:
        def say(self, stage, done=0, total=0):
            said.append((stage, done, total))

    jobs.run_calculation(job_id, users["director"], reporter=Spy())

    stages = [stage for stage, _, _ in said]
    assert any("абел" in s for s in stages), stages       # собираем табели
    assert any("читаем" in s for s in stages), stages      # считаем сотрудников
    assert any("аписыва" in s for s in stages), stages     # записываем ведомость
    # Счётчик людей доходит до общего числа, а не замирает на первом.
    counted = [(done, total) for stage, done, total in said if total]
    assert counted and counted[-1][0] == counted[-1][1] > 0


# --- отказы: права, регистры, заморозка --------------------------------------


def test_the_task_rechecks_the_right_to_calculate(clean_payruns):
    """Право проверяется в момент исполнения, а не в момент нажатия кнопки."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["manager"])

    jobs.run_job(job_id, users["manager"])

    row = job_row(dsn, job_id)
    assert row["status"] == "failed"
    # Именно отказ по праву, дословно. Раньше здесь искалось «прав» или «роли» —
    # и тест оставался зелёным с выброшенной проверкой прав: расчёт доходил до
    # регистров, а их отказ тоже говорит «недоступные вашей роли». Зелёный не по
    # той причине, по которой считался зелёным.
    assert "Расчёт периода не входит в права" in row["error"]
    assert "регистр" not in row["error"].lower()
    assert payslip_count(dsn) == 0


def test_the_task_rechecks_visible_ledgers(clean_payruns):
    """Бухгалтеру фон не даёт того, чего не даёт синхронный расчёт."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["accountant"])

    jobs.run_job(job_id, users["accountant"])

    row = job_row(dsn, job_id)
    assert row["status"] == "failed"
    assert "регистр" in row["error"].lower()
    assert payslip_count(dsn) == 0


def test_an_approved_period_is_not_recalculated_by_the_queue(clean_payruns):
    """Период утвердили, пока задача стояла в очереди: побеждает утверждение."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)

    first = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])
    jobs.run_job(first, users["director"])
    before = raw(
        dsn, "select id, amount from pay_components order by id"
    )
    assert before

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("update payruns set status = 'approved'")

    late = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])
    jobs.run_job(late, users["director"])

    row = job_row(dsn, late)
    assert row["status"] == "failed"
    assert "утверждён" in row["error"].lower()
    # Ни одна цифра закрытого периода не поехала.
    assert raw(dsn, "select id, amount from pay_components order by id") == before


# --- права роли приложения на очередь ----------------------------------------


def test_the_queue_tables_are_closed_to_the_app_role(clean_payruns):
    """Роль приложения ставит задачи, но не читает чужую очередь целиком."""
    import psycopg

    dsn = clean_payruns
    with psycopg.connect(dsn) as conn:
        conn.execute("set local role app_user")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select payload from django_q_ormq").fetchall()
        conn.rollback()

        conn.execute("set local role app_user")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select count(*) from django_q_task").fetchall()
        conn.rollback()

        # А поставить задачу — может: иначе фоновый расчёт не запустился бы.
        conn.execute("set local role app_user")
        new_id = conn.execute(
            "insert into django_q_ormq (key, payload, lock) values ('t', 'x', now()) returning id"
        ).fetchone()[0]
        assert new_id
        conn.rollback()


# --- страница периода --------------------------------------------------------


def progress_block(html: str) -> str:
    found = re.search(r'<section class="progress".*?</section>', html, re.S)
    return found.group(0) if found else ""


def test_the_button_queues_a_job_and_the_page_shows_progress(client, clean_payruns):
    """Нажатие возвращает страницу сразу, а не держит человека до конца расчёта."""
    from django.test import override_settings

    dsn = clean_payruns
    login_as(client, "director")
    url = period_url(client)

    with override_settings(PAYRUN_BACKGROUND=True):
        response = client.post(url + "calculate/", follow=True)
    assert response.status_code == 200

    jobs_in_db = raw(dsn, "select status::text, background from payrun_jobs")
    assert jobs_in_db == [("queued", True)]
    block = progress_block(body(response))
    assert block, "на странице нет блока прогресса"
    assert "очеред" in block.lower()


def test_a_second_click_does_not_queue_a_second_job(client, clean_payruns):
    """Ключевое требование задачи: повторный запуск не плодит ведомости."""
    from django.test import override_settings

    dsn = clean_payruns
    login_as(client, "director")
    url = period_url(client)

    with override_settings(PAYRUN_BACKGROUND=True):
        client.post(url + "calculate/", follow=True)
        again = client.post(url + "calculate/")

    assert again.status_code == 409
    assert raw(dsn, "select count(*) from payrun_jobs")[0][0] == 1
    assert "уже" in body(again).lower()


def test_the_status_answers_json_for_the_page(client, clean_payruns):
    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    make_job(dsn, tenant_id=tenant_id, actor_id=users["director"], status="running")

    login_as(client, "director")
    url = period_url(client)
    response = client.get(url + "calculate/status/")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    assert response.json()["status"] == "running"
    assert response.json()["finished"] is False


def test_a_stuck_job_says_so_instead_of_pretending(client, clean_payruns):
    """Молчаливой подмены нет: задача стоит — страница объясняет, что делать."""
    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    make_job(
        dsn, tenant_id=tenant_id, actor_id=users["director"], created_shift=3600,
    )

    login_as(client, "director")
    response = client.get(period_url(client))
    block = progress_block(body(response))

    assert "рабочий процесс очереди" in block.lower()
    assert "прямо сейчас" in block.lower()


def test_calculating_now_takes_over_the_stuck_job(client, clean_payruns):
    """«Посчитать прямо сейчас» доводит зависшее задание, а не заводит второе."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(
        dsn, tenant_id=tenant_id, actor_id=users["director"], created_shift=3600,
    )

    login_as(client, "director")
    url = period_url(client)
    response = client.post(url + "calculate/", {"inline": "1"}, follow=True)

    assert response.status_code == 200
    assert payslip_count(dsn) > 0
    assert raw(dsn, "select count(*) from payrun_jobs")[0][0] == 1
    row = job_row(dsn, job_id)
    assert row["status"] == "done"
    assert row["background"] is False

    # Задача из очереди приедет позже и не должна пересчитывать заново.
    counted = payslip_count(dsn)
    jobs.run_job(job_id, users["director"])
    assert payslip_count(dsn) == counted


def test_with_the_queue_switched_off_the_page_calculates_as_before(client, clean_payruns):
    """Синхронный путь остаётся первым классом, а не запасным выходом."""
    from django.test import override_settings

    dsn = clean_payruns
    login_as(client, "director")
    url = period_url(client)

    with override_settings(PAYRUN_BACKGROUND=False):
        response = client.post(url + "calculate/", follow=True)

    assert response.status_code == 200
    assert payslip_count(dsn) > 0
    assert raw(dsn, "select status::text, background from payrun_jobs") == [("done", False)]
    # Прогресса на странице нет: обещать его синхронному расчёту незачем.
    assert not progress_block(body(response))


def test_the_button_promises_what_will_actually_happen(client, clean_payruns):
    """Обещание под кнопкой совпадает с режимом расчёта, а не отстаёт от него.

    «Считается сразу» при включённой очереди — неправда в тот самый момент,
    когда человек решает, ждать ему на странице или уйти. Текст жил в шаблоне
    строкой и пережил появление очереди незамеченным.
    """
    from django.test import override_settings

    login_as(client, "director")
    url = period_url(client)

    with override_settings(PAYRUN_BACKGROUND=True):
        html = body(client.get(url))
    assert "уйдёт в очередь" in html
    assert "Считается сразу" not in html

    with override_settings(PAYRUN_BACKGROUND=False):
        html = body(client.get(url))
    assert "Считается сразу" in html
    assert "уйдёт в очередь" not in html


def test_the_finished_job_stops_the_page_from_polling(client, clean_payruns):
    """Расчёт кончился — страница показывает ведомость, а не вечную полосу."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["director"])
    jobs.run_job(job_id, users["director"])

    login_as(client, "director")
    response = client.get(period_url(client))
    html = body(response)

    assert not progress_block(html), "задание завершено, полосе прогресса взяться неоткуда"
    assert "1 951 806,13" in html.replace(" ", " ")


def test_the_failed_job_shows_its_reason_on_the_page(client, clean_payruns):
    """Отказ фоновой задачи виден человеку тем же текстом, что и синхронный."""
    from payrun import jobs

    dsn = clean_payruns
    tenant_id, users = tenant_and_users(dsn)
    job_id = make_job(dsn, tenant_id=tenant_id, actor_id=users["accountant"])
    jobs.run_job(job_id, users["accountant"])
    assert job_row(dsn, job_id)["status"] == "failed"

    login_as(client, "accountant")
    html = body(client.get(period_url(client)))
    assert "регистр" in html.lower()


def test_a_job_of_another_tenant_is_invisible(client, clean_payruns):
    """Задание соседа не видно ни на странице, ни в ответе о состоянии."""
    dsn = clean_payruns
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        other = conn.execute(
            """insert into tenants (code, title, country_code, base_currency)
               values ('xx-dev', 'Сосед', 'XX', 'XXX') returning id"""
        ).fetchone()[0]
        conn.execute(
            "insert into payrun_jobs (tenant_id, period, status) values (%s, %s, 'running')",
            (other, JUNE),
        )

    login_as(client, "director")
    response = client.get(period_url(client) + "calculate/status/")
    assert response.json()["status"] is None

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("delete from payrun_jobs where tenant_id = %s", (other,))
        conn.execute("delete from tenants where id = %s", (other,))


def test_the_stale_threshold_is_a_setting_not_a_guess(clean_payruns):
    """Порог «задачу не взяли» — настройка, а не число, вписанное в шаблон."""
    from django.conf import settings

    assert isinstance(settings.PAYRUN_QUEUE_STALE_SECONDS, int)
    assert settings.PAYRUN_QUEUE_STALE_SECONDS > 0
    assert timedelta(seconds=settings.PAYRUN_QUEUE_STALE_SECONDS) < timedelta(minutes=5)
