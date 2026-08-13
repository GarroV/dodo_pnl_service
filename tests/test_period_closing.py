"""Учётный месяц закрывается сам, вместе с утверждением расчёта (T094).

Что здесь проверяется и почему именно так.

**Цикл обязан замыкаться без ещё одной кнопки.** До T094 утверждение ставило
`payruns.status = approved`, а `periods.status` оставался `open` навсегда: в
продукте не было ни одного пути, который его меняет. Список месяцев показывал
«Открыт» у месяца, который закрыли, — и это не косметика, а неправда о
состоянии, читаемая с первого экрана продукта.

**Состояние месяца держит триггер, а не приложение.** По тому же доводу, что в
`0041`: политики не действуют на суперпользователя, а «месяц закрыт тогда и
только тогда, когда расчёт утверждён» — утверждение о данных, а не о правах.
Поэтому проверки бьют и по базе напрямую, и **владельцем таблиц** тоже.

**Гарантии T023 и T025 обязаны остаться.** Закрытый месяц не добавляет
запретов и не снимает их: запись в утверждённый расчёт по-прежнему отвергает
заморозка, а откат по-прежнему требует причины. На это здесь отдельные тесты —
иначе «починили состояние, сломали заморозку» прошло бы незамеченным.

Тесты доступа ходят ролью `app_user` (`as_app_user`): владелец таблиц и
суперпользователь обходят RLS.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    JULY,
    JUNE,
    T1,
    USER_DIRECTOR,
    as_app_user,
    body,
    login_as,
    period_url,
    wipe_payruns,
)
from test_payrun_approval import reopen
from test_payrun_lifecycle import new_payrun, payrun_in, set_status, status_of

REASON = "пересчитать июнь: ошиблись в часах"


def month(conn, *, tenant: str = T1, period: str = JUNE):
    """Состояние учётного месяца целиком: статус, когда закрыт и кем."""
    return conn.execute(
        "select status, closed_at, closed_by from periods "
        "where tenant_id = %s and period = %s",
        (tenant, period),
    ).fetchone()


def month_status(conn, **kwargs) -> str:
    row = month(conn, **kwargs)
    assert row is not None, "учётного месяца нет в базе"
    return row[0]


# --- сторона базы -------------------------------------------------------------


def test_approving_the_payrun_closes_the_month(db):
    """Главное правило задачи: утверждение расчёта закрывает месяц само."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert month_status(conn) == "open"
        payrun_in(conn, "approved")

        status, closed_at, closed_by = month(conn)
        assert status == "closed", "месяц остался открытым при утверждённом расчёте"
        assert closed_at is not None, "закрытый месяц без времени закрытия"
        assert str(closed_by) == USER_DIRECTOR, (
            "закрыл месяц тот, кто утвердил расчёт, — а в строке кто-то другой"
        )


def test_reopening_the_payrun_opens_the_month_again(db):
    """Откат возвращает месяц в открытые: «закрыт» — состояние, а не история.

    Иначе «закрыт» означало бы «когда-то утверждали», и месяц, который прямо
    сейчас пересчитывают, читался бы как законченный. История отката и так
    есть — в журнале переходов.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        assert month_status(conn) == "closed"

        reopen(conn, payrun_id, REASON)

        status, closed_at, closed_by = month(conn)
        assert status == "open", "месяц остался закрытым после отката расчёта"
        assert closed_at is None and closed_by is None, (
            "у открытого месяца остались следы закрытия"
        )


def test_recalculating_after_a_reopen_leaves_the_month_open(db):
    """Пересчёт месяц не закрывает: закрывает только утверждение."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "reopened")
        set_status(conn, payrun_id, "calculated")

        assert status_of(conn, payrun_id) == "calculated"
        assert month_status(conn) == "open"


def test_the_month_closes_for_the_table_owner_too(db):
    """Держит триггер, а не политика: политику владелец таблиц обходит."""
    payrun_in(db, "approved")
    assert month_status(db) == "closed"


def test_the_month_state_cannot_be_set_by_hand(db):
    """Состояние месяца задаёт расчёт. Прямая правка отклоняется.

    Без этого запрета `periods.status` остаётся вторым источником истины о том
    же самом: его можно поставить в «закрыт» при непосчитанном расчёте, и
    список месяцев соврёт снова — только теперь с чужой руки.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        with pytest.raises(psycopg.Error) as caught:
            with conn.transaction():
                conn.execute(
                    "update periods set status = 'closed' "
                    "where tenant_id = %s and period = %s",
                    (T1, JUNE),
                )
        assert "расч" in str(caught.value), (
            f"отказ не объясняет, чем задаётся состояние месяца: {caught.value}"
        )
        assert month_status(conn) == "open"


def test_the_month_state_cannot_be_set_by_hand_by_the_owner_either(db):
    """Тот же запрет владельцу таблиц: иначе он держался бы на роли соединения."""
    with pytest.raises(psycopg.Error):
        with db.transaction():
            db.execute(
                "update periods set status = 'closed' where tenant_id = %s and period = %s",
                (T1, JUNE),
            )
    assert month_status(db) == "open"


def test_other_columns_of_the_month_stay_editable(db):
    """Запрет ровно на состояние, а не на строку месяца целиком."""
    db.execute(
        "update periods set closed_at = now() where tenant_id = %s and period = %s",
        (T1, JUNE),
    )
    assert month_status(db) == "open"


def test_a_payrun_without_a_month_row_still_approves(db):
    """Расчёт есть, строки месяца нет — утверждение проходит, синхронизировать нечего.

    Так бывает вне продукта: в самом продукте расчёт заводится со страницы
    месяца, то есть строка месяца существует всегда. Падать здесь значило бы
    ломать обслуживание ради состояния, которого нет.
    """
    assert month(db, period=JULY) is None, "фикстура завела июль — тест проверяет не то"
    payrun_id = payrun_in(db, "approved", period=JULY)
    assert status_of(db, payrun_id) == "approved"


def test_the_month_of_another_partner_stays_untouched(db):
    """Закрывается месяц того партнёра, чей расчёт утвердили, и только он."""
    other = "11111111-1111-1111-1111-111111111112"
    db.execute(
        "insert into periods (tenant_id, period, status) values (%s, %s, 'open')",
        (other, JUNE),
    )
    payrun_in(db, "approved")

    assert month_status(db) == "closed"
    assert month_status(db, tenant=other) == "open", "закрыт месяц чужого партнёра"


# --- гарантии, которые обязаны остаться (T023, T025) --------------------------


def test_closing_the_month_does_not_loosen_the_freeze(db):
    """Утверждённый расчёт по-прежнему не принимает записи (T023).

    Владельцем таблиц: заморозку держит триггер, и проверять её под ролью,
    которую и так не пускают политики, значило бы проверять не то.
    """
    employee_id = db.execute(
        "insert into employees (tenant_id, external_id, first_name, last_name) "
        "values (%s, 'ext-t094', 'Тест', 'Тестов') returning id",
        (T1,),
    ).fetchone()[0]
    payrun_id = payrun_in(db, "approved")
    assert month_status(db) == "closed"

    with pytest.raises(psycopg.Error) as caught:
        with db.transaction():
            db.execute(
                "insert into payslips (tenant_id, payrun_id, employee_id) values (%s, %s, %s)",
                (T1, payrun_id, employee_id),
            )
    assert "утвержд" in str(caught.value)


def test_a_reopen_without_a_reason_leaves_the_month_closed(db):
    """Откат без причины отвергнут — и месяц не приоткрылся заодно."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                reopen(conn, payrun_id)

        assert status_of(conn, payrun_id) == "approved"
        assert month_status(conn) == "closed", (
            "отказ отката оставил месяц открытым: состояния разъехались"
        )


def test_an_illegal_transition_does_not_move_the_month(db):
    """Запрещённый переход не двигает ни расчёт, ни месяц."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = new_payrun(conn)
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                conn.execute(
                    "update payruns set status = 'approved' where id = %s", (payrun_id,)
                )
        assert status_of(conn, payrun_id) == "draft"
        assert month_status(conn) == "open"


# --- месяцы, заведённые до миграции ------------------------------------------


def test_the_migration_closes_months_that_were_already_approved():
    """Правда в списке начинается не со следующего утверждения, а сразу.

    База, в которой месяц уже утверждён, до T094 хранила его открытым. Если бы
    миграция только ставила триггеры, такой месяц остался бы открытым навсегда:
    второго утверждения у него не будет — цикл из `approved` ведёт только в
    откат. Поэтому тест накатывает схему **до** T094, заводит на ней ровно ту
    рассинхронизацию, что была в продукте, и проверяет, что миграция её убрала.
    """
    import psycopg
    from psycopg.conninfo import make_conninfo

    from conftest import ADMIN_DSN, run_manage

    pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"нет доступного Postgres по {ADMIN_DSN}: {exc}")

    import os

    dbname = f"dodo_pnl_test_backfill_{os.getpid()}"
    with admin:
        admin.execute(f'drop database if exists "{dbname}"')
        admin.execute(f'create database "{dbname}"')
    dsn = make_conninfo(ADMIN_DSN, dbname=dbname)

    try:
        run_manage(dsn, "migrate", "core", "0150_payslip_steps", "--no-input")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "insert into tenants "
                "(id, code, title, country_code, base_currency, report_currency) "
                "values (%s, 'rs', 'Партнёр', 'RS', 'RSD', 'EUR')",
                (T1,),
            )
            conn.execute(
                "insert into periods (tenant_id, period, status) values (%s, %s, 'open')",
                (T1, JUNE),
            )
            payrun_id = conn.execute(
                "insert into payruns (tenant_id, period) values (%s, %s) returning id",
                (T1, JUNE),
            ).fetchone()[0]
            for status in ("calculated", "approved"):
                conn.execute(
                    "update payruns set status = %s where id = %s", (status, payrun_id)
                )
            # Ровно то состояние, ради которого миграция написана.
            assert month_status(conn) == "open"

        run_manage(dsn, "migrate", "--no-input")

        with psycopg.connect(dsn, autocommit=True) as conn:
            status, closed_at, _closed_by = month(conn)
            assert status == "closed", (
                "миграция оставила утверждённый месяц открытым — "
                "правда в списке начнётся только со следующего утверждения"
            )
            assert closed_at is not None
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin2:
            admin2.execute(f'drop database if exists "{dbname}" with (force)')


# --- сторона экрана -----------------------------------------------------------


def month_row(html: str) -> str:
    """Строка июня в списке месяцев — как её читает человек."""
    import re

    match = re.search(r"<tr>(?:(?!</tr>).)*?июн(?:(?!</tr>).)*?</tr>", html, re.S | re.I)
    assert match, f"в списке месяцев нет строки июня:\n{html}"
    return match.group(0)


def test_the_month_list_says_closed_after_approval(client, web_env):
    """Проверка задачи целиком: список месяцев показывает правду сам.

    Ни одного действия, кроме тех трёх, что обещает онбординг: посчитали,
    утвердили — месяц закрыт. Отдельной кнопки «закрыть период» в продукте нет
    и не появилось.
    """
    wipe_payruns(web_env)
    login_as(client, "director")
    url = period_url(client)

    assert "Открыт" in month_row(body(client.get("/periods/")))

    assert client.post(url + "calculate/", follow=True).status_code == 200
    assert client.post(url + "approve/", follow=True).status_code == 200

    row = month_row(body(client.get("/periods/")))
    assert "Закрыт" in row, f"месяц утверждён, а список показывает иное: {row}"
    assert "Открыт" not in row

    # И обратно: откат возвращает месяц в работу, список говорит и это.
    assert client.post(
        url + "reopen/", {"reason": REASON}, follow=True
    ).status_code == 200
    row = month_row(body(client.get("/periods/")))
    assert "Открыт" in row, f"месяц открыт заново, а список показывает иное: {row}"

    wipe_payruns(web_env)


def test_the_period_page_agrees_with_the_month_list(client, web_env):
    """Состояние месяца на его странице и в списке — одно и то же состояние."""
    wipe_payruns(web_env)
    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    client.post(url + "approve/", follow=True)

    page = body(client.get(url))
    assert "Закрыт" in page, "страница периода не называет утверждённый месяц закрытым"
    assert "Закрыт" in month_row(body(client.get("/periods/")))

    wipe_payruns(web_env)
