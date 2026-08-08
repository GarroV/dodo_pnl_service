"""Права роли что-то решают (T064).

Суть дефекта. У роли есть `permissions`, сид их заполняет, `Principal` их
загружает — и на этом всё: ни одна проверка на них не смотрела. Проверено
грепом по `src/web`, `src/timesheets`, `src/payrun` и фактически в браузере:
администратор сети, у которого нет ни `timesheet.edit`, ни `payrun.calculate`,
**отредактировал ячейку табеля**. А отказ на «Посчитать» он получал не по праву,
а по случайности: ему в сиде выдан только официальный регистр, и срабатывала
проверка регистров. Спека при этом говорит прямо: «Администратор сети:
справочники, пресеты правил, роли; данные расчётов не правит».

Поэтому здесь у администратора **все три регистра** (фикстура `conftest`) — и
отказ он всё равно обязан получить. Иначе проверка снова доказывала бы не то.

Два контура, и оба обязаны быть:

* **база** — ограничивающие политики на запись: роль без права физически не
  запишет строку, даже если новый экран забудет спросить. Это тот же довод, что
  у D014 про видимость;
* **приложение** — внятный отказ на экране. База отвечает кодом ошибки, из
  которого человеку ничего не понятно, а «нельзя» должно быть сказано словами.
"""
from __future__ import annotations

import pytest

from conftest import (
    JUNE,
    T1,
    U_NS1,
    USER_ADMIN,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
    body,
    login_as,
    period_url,
)


def denied():
    import psycopg

    return psycopg.errors.InsufficientPrivilege


def make_employee(conn, ext_id: str) -> str:
    employee = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Тестов') returning id""",
        (T1, ext_id),
    ).fetchone()[0]
    group = conn.execute(
        """insert into employee_groups (tenant_id, code, title, scheme)
           values (%s, 'g-perm', 'Группа', 'hourly')
           on conflict (tenant_id, code) do update set title = excluded.title
           returning id""",
        (T1,),
    ).fetchone()[0]
    conn.execute(
        """insert into employment_terms
               (tenant_id, employee_id, group_id, unit_id, base_rate, valid_from)
           values (%s, %s, %s, %s, 100, %s)""",
        (T1, employee, group, U_NS1, JUNE),
    )
    return employee


def make_timesheet(conn, ext_id: str) -> str:
    return conn.execute(
        """insert into timesheets (tenant_id, employee_id, unit_id, period, norm_hours)
           values (%s, %s, %s, %s, 176) returning id""",
        (T1, make_employee(conn, ext_id), U_NS1, JUNE),
    ).fetchone()[0]


# --- табель: право timesheet.edit --------------------------------------------


def test_a_role_without_the_right_cannot_write_a_timesheet(db):
    """Главная проверка: пишет не тот, кто видит, а тот, кому положено."""
    sheet = make_timesheet(db, "ts-perm")

    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(denied()):
            conn.execute(
                "update timesheets set hours = '{\"regular\": 8}'::jsonb where id = %s", (sheet,)
            )
        conn.execute("rollback to savepoint attempt")


def test_a_role_without_the_right_cannot_write_a_day(db):
    """Подневное хранение — тот же табель, только другой таблицей."""
    sheet = make_timesheet(db, "ts-day-perm")

    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(denied()):
            conn.execute(
                """insert into timesheet_days
                       (tenant_id, timesheet_id, work_date, hour_type, hours)
                   values (%s, %s, '2026-06-02', 'regular', 8)""",
                (T1, sheet),
            )
        conn.execute("rollback to savepoint attempt")


def test_the_right_is_what_allows_it(db):
    """Контроль: у управляющего право есть, и он пишет ту же ячейку."""
    sheet = make_timesheet(db, "ts-ok")

    with as_app_user(db, USER_MANAGER) as conn:
        assert conn.execute(
            "update timesheets set hours = '{\"regular\": 8}'::jsonb where id = %s", (sheet,)
        ).rowcount == 1


def test_reading_the_timesheet_is_not_taken_away(db):
    """Право на правку — не право на просмотр: администратор табель видит."""
    make_timesheet(db, "ts-read")

    with as_app_user(db, USER_ADMIN) as conn:
        assert conn.execute("select count(*) from timesheets").fetchone()[0] == 1


# --- расчёт: право payrun.calculate ------------------------------------------


@pytest.mark.parametrize("table, columns, values", [
    ("payruns", "(tenant_id, period)", "(%s, '2026-07-01')"),
    ("payslips", "(tenant_id, payrun_id, employee_id)", None),
])
def test_a_role_without_the_right_cannot_write_the_payrun(db, table, columns, values):
    """Расчёт пишет в четыре таблицы, и закрыты должны быть все."""
    if table == "payruns":
        sql = f"insert into {table} {columns} values {values}"
        params = (T1,)
    else:
        payrun = db.execute(
            """insert into payruns (tenant_id, period) values (%s, %s)
               on conflict (tenant_id, period) do update set period = excluded.period
               returning id""",
            (T1, JUNE),
        ).fetchone()[0]
        sql = f"insert into {table} {columns} values (%s, %s, %s)"
        params = (T1, payrun, make_employee(db, f"pay-{table}"))

    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(denied()):
            conn.execute(sql, params)
        conn.execute("rollback to savepoint attempt")


def test_a_role_without_the_right_cannot_write_components_or_totals(db):
    payrun = db.execute(
        """insert into payruns (tenant_id, period) values (%s, %s)
           on conflict (tenant_id, period) do update set period = excluded.period
           returning id""",
        (T1, JUNE),
    ).fetchone()[0]
    payslip = db.execute(
        """insert into payslips (tenant_id, payrun_id, employee_id)
           values (%s, %s, %s) returning id""",
        (T1, payrun, make_employee(db, "pay-parts")),
    ).fetchone()[0]

    with as_app_user(db, USER_ADMIN) as conn:
        for sql, params in (
            ("""insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
                values (%s, %s, 'hours.regular', 'Часы', 10, 'official')""", (T1, payslip)),
            ("insert into payslip_totals (tenant_id, payslip_id, net) values (%s, %s, 10)",
             (T1, payslip)),
        ):
            conn.execute("savepoint attempt")
            with pytest.raises(denied()):
                conn.execute(sql, params)
            conn.execute("rollback to savepoint attempt")


def test_the_one_with_the_right_still_calculates(db):
    """Контроль: директору расчёт по-прежнему доступен целиком."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun = conn.execute(
            """insert into payruns (tenant_id, period) values (%s, '2026-05-01')
               returning id""",
            (T1,),
        ).fetchone()[0]
        assert payrun is not None


def test_reading_the_payrun_is_not_taken_away(db):
    """Администратор расчёт видит — он просто его не правит."""
    db.execute("insert into payruns (tenant_id, period) values (%s, '2026-04-01')", (T1,))

    with as_app_user(db, USER_ADMIN) as conn:
        assert conn.execute("select count(*) from payruns").fetchone()[0] >= 1


# --- доказательство, что режет политика, а не случайность --------------------


def test_the_refusal_is_the_policy_and_not_luck(db):
    """Снимаем ограничивающие политики — запись обязана пройти.

    Иначе нельзя отличить «закрыто правом» от «не записалось по другой
    причине»: у администратора все регистры и все точки, и больше мешать ему
    нечему.
    """
    sheet = make_timesheet(db, "ts-damage")
    db.execute("savepoint before_damage")
    db.execute("drop policy timesheet_edit_update on timesheets")
    try:
        with as_app_user(db, USER_ADMIN) as conn:
            assert conn.execute(
                "update timesheets set hours = '{\"regular\": 8}'::jsonb where id = %s", (sheet,)
            ).rowcount == 1, "без политики администратор обязан записать ячейку"
    finally:
        db.execute("rollback to savepoint before_damage")

    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(denied()):
            conn.execute(
                "update timesheets set hours = '{\"regular\": 8}'::jsonb where id = %s", (sheet,)
            )
        conn.execute("rollback to savepoint attempt")


def test_a_role_without_a_membership_has_no_rights(db):
    """Контекста нет — прав нет. Проверка не должна давать «true по умолчанию»."""
    with as_app_user(db, None) as conn:
        assert conn.execute(
            "select app_has_permission(%s, 'timesheet.edit')", (T1,)
        ).fetchone()[0] is False


# =============================================================================
# То же самое на экране
# =============================================================================
# База отвечает кодом ошибки, из которого человеку ничего не понятно. Отказ
# обязан быть сказан словами — так же, как уже сделано для регистров.


def test_admin_is_refused_the_calculation_by_the_right(client, web_env):
    """Отказ — про право, а не про регистры.

    Раньше администратор получал отказ по случайности: ему выдан только
    официальный регистр, и срабатывала проверка регистров. Ту же плашку он
    получил бы, даже если бы право у него было, — значит она ничего не
    доказывала.
    """
    login_as(client, "admin")
    response = client.post(period_url(client) + "calculate/")
    text = body(response)

    assert response.status_code == 403
    assert "Расчёт периода" in text
    assert "регистр" not in text.split("Расчёт периода")[1][:400], (
        "отказ обязан объяснять право, а не видимость регистров"
    )


def test_director_still_calculates(client, web_env):
    """Контроль: право у директора есть, кнопка работает."""
    login_as(client, "director")
    response = client.post(period_url(client) + "calculate/")
    assert response.status_code == 302


def grid_url(client) -> str:
    import re

    match = re.search(r"([0-9a-f-]{36})", period_url(client))
    return f"/timesheets/{match.group(1)}/"


def first_cell(html: str) -> tuple[str, str]:
    """Первая ячейка сетки: строка табеля и тип часа."""
    import re

    match = re.search(r'data-row="([0-9a-f-]+)" data-kind="([a-z_]+)"', html)
    assert match, "на странице табеля нет ни одной ячейки"
    return match.group(1), match.group(2)


def test_admin_cannot_edit_a_cell_from_the_screen(client, web_env):
    """Тот самый случай: администратор правил ячейку табеля, и правка проходила."""
    import psycopg

    login_as(client, "director")
    url = grid_url(client)
    row, kind = first_cell(body(client.get(url)))

    with psycopg.connect(web_env) as conn:
        before = conn.execute("select hours from timesheets where id = %s", (row,)).fetchone()[0]

    login_as(client, "admin")
    response = client.post(f"{url}cell/", {"row": row, "kind": kind, "hours": "7"})
    assert response.status_code == 403
    assert "прав" in response.content.decode().lower()

    with psycopg.connect(web_env) as conn:
        after = conn.execute("select hours from timesheets where id = %s", (row,)).fetchone()[0]
    assert after == before, "ячейка изменилась — отказ был бумажным"


def test_manager_still_edits_a_cell(client, web_env):
    """Контроль: у управляющего право есть, ячейка пишется.

    Строка возвращается к исходному виду. База `web_env` живёт весь прогон, и
    оставленные здесь 7 часов сдвигали расчёт периода на 2597,00 — тест,
    знающий эталонную сумму, падал не по своей вине. Найдено при T020.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    login_as(client, "manager")
    url = grid_url(client)
    row, kind = first_cell(body(client.get(url)))

    with psycopg.connect(web_env) as conn:
        before = conn.execute(
            "select hours, insured_hours from timesheets where id = %s", (row,)
        ).fetchone()

    try:
        response = client.post(f"{url}cell/", {"row": row, "kind": kind, "hours": "7"})
        assert response.status_code == 200
    finally:
        with psycopg.connect(web_env, autocommit=True) as conn:
            conn.execute("delete from timesheet_days where timesheet_id = %s", (row,))
            conn.execute(
                "update timesheets set hours = %s, insured_hours = %s where id = %s",
                (Jsonb(before[0]), before[1], row),
            )


# =============================================================================
# Экран не предлагает того, что сам же отвергнет (T072)
# =============================================================================
# Оба контура выше работают правильно: база не пускает, представление объясняет.
# Дефект в другом — узнать о запрете можно было только совершив действие.
# Администратор видел редактируемую сетку 35×6 и кнопку «Посчитать», управляющий
# — кнопку «Посчитать»; оба получали 403 уже после нажатия. Это не замена
# серверным проверкам: они остаются на месте, и тесты выше их и стерегут.


@pytest.mark.parametrize("code", ["admin", "manager"])
def test_a_role_without_the_right_does_not_see_the_calculate_button(client, web_env, code):
    login_as(client, code)
    html = body(client.get(period_url(client)))

    assert "Посчитать период" not in html, "кнопка предлагает действие, которое даст 403"
    assert "calculate/" not in html
    # Спрятать молча нельзя: пропавшая кнопка читается как поломка. Объяснение —
    # теми же словами, что и отказ на самом действии.
    assert "Расчёт периода не входит в права" in html


@pytest.mark.parametrize("code", ["director", "accountant"])
def test_a_role_with_the_right_still_sees_the_calculate_button(client, web_env, code):
    login_as(client, code)
    html = body(client.get(period_url(client)))

    assert "Посчитать период" in html
    assert "Расчёт периода не входит в права" not in html


def test_a_role_without_the_right_gets_a_read_only_grid(client, web_env):
    """Сетка на чтение: полей ввода нет, данные на месте.

    Проверяется именно пара «нет правки — есть данные»: право на правку табеля
    не отнимает права его видеть, и подменять запрет пустой страницей нельзя.
    """
    import re

    login_as(client, "director")
    editable = body(client.get(grid_url(client)))
    login_as(client, "admin")
    readonly = body(client.get(grid_url(client)))

    assert 'class="cell"' in editable and "hx-post" in editable, (
        "у роли с правом сетка обязана остаться редактируемой — иначе тест ниже "
        "доказывал бы, что экран просто сломан"
    )
    assert 'class="cell"' not in readonly, "поле ввода предлагает правку, которая даст 403"
    assert "hx-post" not in readonly

    # Тех же людей и те же часы администратор видит по-прежнему.
    assert readonly.count("<tr") == editable.count("<tr")
    totals = [re.search(r'id="grand-total">([^<]*)<', page) for page in (editable, readonly)]
    assert all(totals), "на странице табеля нет общего итога — сравнивать нечего"
    assert totals[0].group(1) == totals[1].group(1)
    assert "правка табеля не входит в права" in readonly.lower()


def test_a_role_with_the_right_still_gets_an_editable_grid(client, web_env):
    """Контроль: у управляющего право есть, и сетка у него та же, что была."""
    login_as(client, "manager")
    html = body(client.get(grid_url(client)))

    assert 'class="cell"' in html and "hx-post" in html
    assert "правка табеля не входит в права" not in html.lower()
