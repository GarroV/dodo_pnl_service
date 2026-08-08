"""Модель периода расчёта и переходы статусов (T023).

Что здесь проверяется и почему именно так:

1. **Каждый переход, включая запрещённые.** Разрешённых ровно четыре, остальные
   пары статусов перебираются параметризацией — тест не про «работает счастливый
   путь», а про то, что всё прочее база отвергает.
2. **Запись в утверждённый расчёт отклоняется базой.** Все такие тесты ходят
   ролью `app_user` (`as_app_user`): владелец таблиц и суперпользователь обходят
   RLS, и на этом в проекте уже прожил незамеченным дефект видимости регистров.
   Отдельным тестом показано, что заморозку держит **триггер**, а не политика:
   она отказывает и суперпользователю тоже, а политика бы не отказала.
3. **Журнал переходов пишет база.** Приложение его не наполняет, поэтому
   перехода без записи не бывает ни на одном пути записи.

Тесты гоняются на живом Postgres (фикстуры `db` и `web_env` в conftest); без
Postgres пропускаются вместе с остальными тестами схемы.
"""
from __future__ import annotations

import pytest

from conftest import (
    JUNE,
    T1,
    USER_DIRECTOR,
    USER_OTHER,
    as_app_user,
    body,
    login_as,
    period_url,
    wipe_payruns,
)

# Все значения типа payrun_status. `paid` в enum есть с самого начала, экрана
# выплаты нет ни в одной задаче очереди — он остаётся недостижимым, и это
# проверяется: любой переход в него отвергается.
ALL_STATUSES = ("draft", "calculated", "approved", "reopened", "paid")

ALLOWED = (
    ("draft", "calculated"),
    ("calculated", "approved"),
    ("approved", "reopened"),
    ("reopened", "calculated"),
)

# Как добраться до статуса от только что заведённого расчёта. `paid` в списке
# отсутствует именно потому, что дороги к нему нет.
PATH_TO = {
    "draft": (),
    "calculated": ("calculated",),
    "approved": ("calculated", "approved"),
    "reopened": ("calculated", "approved", "reopened"),
}

FORBIDDEN = [
    (source, target)
    for source in PATH_TO
    for target in ALL_STATUSES
    if source != target and (source, target) not in ALLOWED
]


# --- помощники ---------------------------------------------------------------


def new_payrun(conn, *, tenant: str = T1, period: str = JUNE) -> str:
    return conn.execute(
        "insert into payruns (tenant_id, period) values (%s, %s) returning id",
        (tenant, period),
    ).fetchone()[0]


# Чем объясняются откаты, сделанные тестом просто чтобы добраться до статуса.
# Сама проверка причины живёт в `test_payrun_approval.py` (T025).
DEFAULT_REASON = "перевод в тесте"


def reason_required(conn, status: str) -> bool:
    """Требует ли база причину на этот переход. Спрашиваем у неё, а не помним."""
    return conn.execute(
        "select payrun_reason_required(%s::payrun_status)", (status,)
    ).fetchone()[0]


def current_reason(conn) -> str:
    return conn.execute(
        "select coalesce(current_setting('app.transition_reason', true), '')"
    ).fetchone()[0]


def set_status(conn, payrun_id: str, *statuses: str) -> None:
    """Перевести расчёт по статусам, подставляя причину там, где база её требует.

    Причину, выставленную самим тестом, не затирает: тест, который проверяет
    журнал, ставит её сам, и подмена превратила бы его в проверку этой строки.
    """
    for status in statuses:
        if reason_required(conn, status) and not current_reason(conn).strip():
            conn.execute(
                "select set_config('app.transition_reason', %s, true)", (DEFAULT_REASON,)
            )
        conn.execute(
            "update payruns set status = %s where id = %s", (status, payrun_id)
        )


def payrun_in(conn, status: str, *, period: str = JUNE) -> str:
    """Расчёт, доведённый до нужного статуса разрешённой дорогой."""
    payrun_id = new_payrun(conn, period=period)
    set_status(conn, payrun_id, *PATH_TO[status])
    return payrun_id


def rejected(conn, sql: str, params=()):
    """Ожидаем отказ базы. Транзакция остаётся рабочей — дальше можно проверять.

    Точка сохранения нужна потому, что ошибка обрывает транзакцию целиком, а
    тесту после отказа надо ещё убедиться, что данные на месте.
    """
    import psycopg

    with pytest.raises(psycopg.Error) as caught:
        with conn.transaction():
            conn.execute(sql, params)
    return caught.value


def status_of(conn, payrun_id: str) -> str:
    return conn.execute(
        "select status from payruns where id = %s", (payrun_id,)
    ).fetchone()[0]


def journal(conn, payrun_id: str) -> list[tuple]:
    # Порядок — по ключу журнала: внутри одной транзакции `now()` у всех записей
    # одинаковый, и сортировка по времени порядок переходов не восстановит.
    return conn.execute(
        """select from_status, to_status, actor_id, reason
             from payrun_transitions where payrun_id = %s order by id""",
        (payrun_id,),
    ).fetchall()


def employee(conn, external_id: str) -> str:
    return conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Тестов') returning id""",
        (T1, external_id),
    ).fetchone()[0]


def make_slip(conn, payrun_id: str) -> dict:
    """Материал для проверки заморозки: заполненная строка, пустая и свободный человек.

    Пустая строка и свободный человек нужны затем, что вставку в утверждённый
    расчёт иначе отвергло бы ограничение уникальности, а не заморозка, — и тест
    проверял бы не то, что заявлено.
    """
    payslip_id = conn.execute(
        """insert into payslips (tenant_id, payrun_id, employee_id)
           values (%s, %s, %s) returning id""",
        (T1, payrun_id, employee(conn, "lc-filled")),
    ).fetchone()[0]
    conn.execute(
        """insert into payslip_totals (payslip_id, tenant_id, net, gross)
           values (%s, %s, 1000, 1200)""",
        (payslip_id, T1),
    )
    component_id = conn.execute(
        """insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
           values (%s, %s, 'hours.regular', 'Часы', 1000, 'official') returning id""",
        (T1, payslip_id),
    ).fetchone()[0]
    bare_id = conn.execute(
        """insert into payslips (tenant_id, payrun_id, employee_id)
           values (%s, %s, %s) returning id""",
        (T1, payrun_id, employee(conn, "lc-bare")),
    ).fetchone()[0]
    return {
        "tenant": T1,
        "payrun": payrun_id,
        "payslip": payslip_id,
        "component": component_id,
        "bare": bare_id,
        "spare": employee(conn, "lc-spare"),
    }


# --- переходы ----------------------------------------------------------------


@pytest.mark.parametrize("source,target", ALLOWED)
def test_allowed_transition_goes_through(db, source, target):
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, source)
        set_status(conn, payrun_id, target)
        assert status_of(conn, payrun_id) == target


@pytest.mark.parametrize("source,target", FORBIDDEN)
def test_forbidden_transition_is_rejected(db, source, target):
    """Запрещён — значит отвергается базой, а статус остаётся прежним."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, source)
        error = rejected(
            conn, "update payruns set status = %s where id = %s", (target, payrun_id)
        )
        assert source in str(error) and target in str(error)
        assert status_of(conn, payrun_id) == source


def test_new_payrun_starts_as_draft(db):
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert status_of(conn, new_payrun(conn)) == "draft"


def test_payrun_cannot_be_created_already_approved(db):
    """Создание сразу утверждённым обошло бы весь цикл — база не даёт."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        error = rejected(
            conn,
            "insert into payruns (tenant_id, period, status) values (%s, %s, 'approved')",
            (T1, JUNE),
        )
        assert "approved" in str(error)


def test_recalculation_is_not_a_transition(db):
    """Пересчёт меняет отметку времени и оставляет статус — это разрешено."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "calculated")
        conn.execute(
            "update payruns set calculated_at = now() where id = %s", (payrun_id,)
        )
        assert status_of(conn, payrun_id) == "calculated"


def test_second_payrun_in_the_same_period_is_rejected(db):
    """Один расчёт на период — значит и один черновик, второго не завести."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        new_payrun(conn)
        error = rejected(
            conn,
            "insert into payruns (tenant_id, period) values (%s, %s)",
            (T1, JUNE),
        )
        assert "payruns_tenant_period_uniq" in str(error)


def test_reopened_is_not_a_return_to_draft(db):
    """Откат виден по статусу: «открывали» и «не считали» — разные состояния."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "reopened")
        assert status_of(conn, payrun_id) == "reopened"
        assert [row[1] for row in journal(conn, payrun_id)] == [
            "draft", "calculated", "approved", "reopened",
        ]


# --- утверждённый расчёт не меняется -----------------------------------------


def test_approved_payrun_rejects_a_column_change(db):
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        error = rejected(
            conn, "update payruns set calculated_at = now() where id = %s", (payrun_id,)
        )
        assert "утверждён" in str(error)


def test_reopening_cannot_smuggle_a_column_change(db):
    """Откат — только смена статуса: правка «заодно» прошла бы мимо журнала."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        error = rejected(
            conn,
            "update payruns set status = 'reopened', calculated_at = now() where id = %s",
            (payrun_id,),
        )
        assert "утверждён" in str(error)
        assert status_of(conn, payrun_id) == "approved"


def test_approved_payrun_cannot_be_deleted(db):
    """Удаление — предельная форма записи, и оно тоже отклоняется."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        error = rejected(conn, "delete from payruns where id = %s", (payrun_id,))
        assert "утверждён" in str(error)
        assert status_of(conn, payrun_id) == "approved"


WRITES = {
    "payslips.insert": """
        insert into payslips (tenant_id, payrun_id, employee_id)
        values (%(tenant)s, %(payrun)s, %(spare)s)""",
    "payslips.update": "update payslips set notes = array['правка'] where id = %(payslip)s",
    "payslips.delete": "delete from payslips where id = %(payslip)s",
    "payslip_totals.insert": """
        insert into payslip_totals (payslip_id, tenant_id, net)
        values (%(bare)s, %(tenant)s, 500)""",
    "payslip_totals.update": "update payslip_totals set net = 999 where payslip_id = %(payslip)s",
    "payslip_totals.delete": "delete from payslip_totals where payslip_id = %(payslip)s",
    "pay_components.insert": """
        insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
        values (%(tenant)s, %(payslip)s, 'bonus', 'Премия', 500, 'official')""",
    "pay_components.update": "update pay_components set amount = 1 where id = %(component)s",
    "pay_components.delete": "delete from pay_components where id = %(component)s",
}


@pytest.mark.parametrize("case", sorted(WRITES))
def test_write_into_an_approved_payrun_is_rejected_by_the_database(db, case):
    """Ключевой тест задачи: ролью приложения, на каждую таблицу расчёта.

    Проверка в Python контуром не является — её обходит любой другой путь
    записи. Поэтому отказ обязан приходить от базы.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "calculated")
        material = make_slip(conn, payrun_id)
        set_status(conn, payrun_id, "approved")

        error = rejected(conn, WRITES[case], material)
        assert "утверждён" in str(error)

        # Данные на месте: отказ, а не «изменено 0 строк».
        assert conn.execute(
            "select count(*) from pay_components where payslip_id = %s",
            (material["payslip"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "select net from payslip_totals where payslip_id = %s", (material["payslip"],)
        ).fetchone()[0] == 1000


@pytest.mark.parametrize("case", sorted(WRITES))
def test_the_same_writes_pass_after_reopening(db, case):
    """Заморозка — про статус, а не про сломанную таблицу."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "calculated")
        material = make_slip(conn, payrun_id)
        set_status(conn, payrun_id, "approved", "reopened")

        conn.execute(WRITES[case], material)


def test_freeze_holds_for_the_table_owner_too(db):
    """Держит триггер, а не политика: суперпользователь RLS обходит, триггер — нет.

    Без этого «утверждённое не меняется» держалось бы на том, каким
    пользователем подключились, — то есть не держалось бы.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "calculated")
        material = make_slip(conn, payrun_id)
        set_status(conn, payrun_id, "approved")

    error = rejected(
        db, "update payslip_totals set net = 1 where payslip_id = %s", (material["payslip"],)
    )
    assert "утверждён" in str(error)


def test_draft_payrun_accepts_writes(db):
    """Обратная сторона: пока расчёт не утверждён, запись идёт как раньше."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        material = make_slip(conn, new_payrun(conn))
        conn.execute(
            "update payslip_totals set net = 7 where payslip_id = %s", (material["payslip"],)
        )
        assert conn.execute(
            "select net from payslip_totals where payslip_id = %s", (material["payslip"],)
        ).fetchone()[0] == 7


# --- журнал переходов --------------------------------------------------------


def test_journal_records_creation_and_every_transition(db):
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        rows = journal(conn, payrun_id)
        assert [(row[0], row[1]) for row in rows] == [
            (None, "draft"), ("draft", "calculated"), ("calculated", "approved"),
        ]
        # Автор берётся из контекста пользователя — того же, на котором стоит RLS.
        assert {str(row[2]) for row in rows} == {USER_DIRECTOR}


def test_journal_carries_the_reason_of_a_single_transition(db):
    """Причина приезжает настройкой транзакции и не липнет к следующему переходу.

    Это и есть заготовка под T025: там останется потребовать её при откате.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        conn.execute(
            "select set_config('app.transition_reason', %s, true)", ("ошиблись в часах",)
        )
        set_status(conn, payrun_id, "reopened")
        set_status(conn, payrun_id, "calculated")

        reasons = {row[1]: row[3] for row in journal(conn, payrun_id)}
        assert reasons["reopened"] == "ошиблись в часах"
        assert reasons["calculated"] is None


def test_journal_is_append_only(db):
    """Историю, которую можно переписать, историей называть нельзя."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "calculated")
        assert "журнал" in str(
            rejected(conn, "update payrun_transitions set reason = 'нет' where payrun_id = %s",
                     (payrun_id,))
        )
        assert "журнал" in str(
            rejected(conn, "delete from payrun_transitions where payrun_id = %s", (payrun_id,))
        )
        assert len(journal(conn, payrun_id)) == 2


def test_journal_of_another_tenant_is_invisible(db):
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "calculated")

    with as_app_user(db, USER_OTHER) as conn:
        assert journal(conn, payrun_id) == []


def test_the_journal_goes_away_with_its_payrun(db):
    """Истории без расчёта не бывает: журнал исчезает вместе с ним.

    Каскад — настоящий, внешним ключом в схеме, а не удалением из приложения:
    «только пополняется» и «исчезает вместе с расчётом» уживаются лишь тогда,
    когда второе делает база сама, внутри удаления самого расчёта.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "calculated")
        assert len(journal(conn, payrun_id)) == 2

        conn.execute("delete from payruns where id = %s", (payrun_id,))
        assert journal(conn, payrun_id) == []


def test_the_seed_runs_over_a_period_that_was_approved():
    """Сид обязан проходить и после того, как период утверждали и открывали.

    Падало: `seed_dev` сносит расчёты через ORM, а Django исполняет каскад **в
    Python** — то есть отдельным `delete` по журналу. Для триггера это прямое
    удаление истории, а не каскад, и он отказывал. После первого же утверждения
    базу разработки и демо нельзя было пересидировать.

    Тест гоняет настоящую команду в подпроцессе на своей базе: воспроизводится
    ровно то, что делает человек руками.
    """
    psycopg = pytest.importorskip("psycopg")

    from conftest import run_manage, temp_database

    with temp_database("payrun_seed") as dsn:
        run_manage(dsn, "seed_dev")

        with psycopg.connect(dsn, autocommit=True) as conn:
            payrun_id = conn.execute(
                "insert into payruns (tenant_id, period) "
                "select id, '2026-06-01' from tenants limit 1 returning id"
            ).fetchone()[0]
            set_status(conn, payrun_id, "calculated", "approved")
            with conn.transaction():
                conn.execute(
                    "select set_config('app.transition_reason', %s, true)", (DEFAULT_REASON,)
                )
                conn.execute(
                    "update payruns set status = 'reopened' where id = %s", (payrun_id,)
                )
            assert conn.execute("select count(*) from payrun_transitions").fetchone()[0] == 4

        run_manage(dsn, "seed_dev")

        with psycopg.connect(dsn) as conn:
            # Расчёт снесён — значит, и история его не пережила.
            assert conn.execute("select count(*) from payruns").fetchone()[0] == 0
            assert conn.execute("select count(*) from payrun_transitions").fetchone()[0] == 0


# --- расчёт и статус ---------------------------------------------------------
# Дальше — живой Django на базе с сидом: расчёт со страницы периода обязан
# оставлять статус, а не молча держать черновик.


@pytest.fixture
def clean_payruns(web_env):
    wipe_payruns(web_env)
    yield web_env
    wipe_payruns(web_env)


def payrun_status(dsn: str) -> str:
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute("select status from payruns").fetchone()[0]


def force_status(dsn: str, status: str) -> None:
    """Перевести расчёт суперпользователем, минуя экраны.

    Причина подставляется там, где её требует база (откат): требование стоит
    триггером, поэтому действует и на суперпользователя тоже — см. T025.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute(
                "select set_config('app.transition_reason', case when "
                "payrun_reason_required(%s::payrun_status) then %s else '' end, true)",
                (status, DEFAULT_REASON),
            )
            conn.execute("update payruns set status = %s", (status,))


def test_calculation_leaves_the_payrun_calculated(client, clean_payruns):
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200
    assert payrun_status(clean_payruns) == "calculated"


def test_recalculation_keeps_the_status(client, clean_payruns):
    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    client.post(url + "calculate/", follow=True)
    assert payrun_status(clean_payruns) == "calculated"


def test_calculation_of_an_approved_period_is_refused_with_words(client, clean_payruns):
    """Отказ объясняет, а не приходит ошибкой драйвера. База остаётся страховкой."""
    import psycopg

    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    force_status(clean_payruns, "approved")

    with psycopg.connect(clean_payruns) as conn:
        before = conn.execute("select count(*) from pay_components").fetchone()[0]

    response = client.post(url + "calculate/", follow=True)
    assert response.status_code == 409
    assert "утвержд" in body(response)

    with psycopg.connect(clean_payruns) as conn:
        assert conn.execute("select count(*) from pay_components").fetchone()[0] == before
    assert payrun_status(clean_payruns) == "approved"


def test_calculation_works_again_after_reopening(client, clean_payruns):
    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    force_status(clean_payruns, "approved")
    force_status(clean_payruns, "reopened")

    assert client.post(url + "calculate/", follow=True).status_code == 200
    assert payrun_status(clean_payruns) == "calculated"


def test_a_period_without_a_payrun_is_untouched(client, clean_payruns):
    """Пустой период — не «утверждённый»: страница открывается как прежде."""
    login_as(client, "director")
    assert client.get(period_url(client)).status_code == 200
