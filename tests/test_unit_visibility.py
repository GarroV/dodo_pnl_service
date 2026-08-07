"""Управляющий точки видит только свою точку (T044).

Суть дефекта. У членства есть `unit_ids` («null = все точки тенанта»),
интерфейс на странице входа обещает управляющему точку NS1, а ведомость
показывала все три: по этому полю не фильтровал никто — ни база, ни
приложение. Обещание было бумажным.

Почему чинится в базе, а не в представлении. D014: разграничение доступа целиком
в RLS, приложение не фильтрует вручную. Фильтр в представлении дал бы второй
источник истины о доступе, и следующий отчёт, забывший его повторить, отдал бы
чужую точку. Здесь `app_unit_ids()` читает то же самое членство, что и
`app_tenant_ids()`, а политики сужают выборку сами.

Проверки идут **ролью `app_user`**. Владелец таблиц политики обходит (а
суперпользователь обходит даже `force row level security`), поэтому написать
политику неправильно и получить зелёный прогон здесь особенно легко.

Что зафиксировано отдельным тестом как решение, а не как случайность:
строка без точки (`unit_id is null`) видна всем, у кого есть доступ к тенанту.
Такая строка не принадлежит другой точке — она не принадлежит никакой, и прятать
её значило бы терять данные молча (`unit_id` обнуляется при удалении точки,
`on delete set null`).
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from conftest import (
    JUNE,
    T1,
    U_BG1,
    U_NS1,
    U_NS2,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
    body,
    login_as,
    period_url,
    wipe_payruns,
)


def make_employee(conn, ext_id: str) -> str:
    return conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', %s) returning id""",
        (T1, ext_id, ext_id),
    ).fetchone()[0]


def make_payslip(conn, ext_id: str, unit_id: str | None, amount: str) -> str:
    """Строка ведомости на точке с одним официальным компонентом."""
    employee = make_employee(conn, ext_id)
    payrun = conn.execute(
        """insert into payruns (tenant_id, period) values (%s, %s)
           on conflict (tenant_id, period) do update set period = excluded.period
           returning id""",
        (T1, JUNE),
    ).fetchone()[0]
    payslip = conn.execute(
        """insert into payslips (tenant_id, payrun_id, employee_id, unit_id)
           values (%s, %s, %s, %s) returning id""",
        (T1, payrun, employee, unit_id),
    ).fetchone()[0]
    conn.execute(
        """insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
           values (%s, %s, 'hours.regular', 'Часы', %s, 'official')""",
        (T1, payslip, amount),
    )
    return payslip


def make_timesheet(conn, ext_id: str, unit_id: str | None) -> str:
    employee = make_employee(conn, ext_id)
    return conn.execute(
        """insert into timesheets (tenant_id, employee_id, unit_id, period, norm_hours)
           values (%s, %s, %s, %s, 176) returning id""",
        (T1, employee, unit_id, JUNE),
    ).fetchone()[0]


def make_term(conn, ext_id: str, unit_id: str | None) -> str:
    employee = make_employee(conn, ext_id)
    group = conn.execute(
        """insert into employee_groups (tenant_id, code, title, scheme)
           values (%s, %s, 'Группа', 'hourly')
           on conflict (tenant_id, code) do update set title = excluded.title
           returning id""",
        (T1, "g-units"),
    ).fetchone()[0]
    return conn.execute(
        """insert into employment_terms
               (tenant_id, employee_id, group_id, unit_id, base_rate, valid_from)
           values (%s, %s, %s, %s, 100, %s) returning id""",
        (T1, employee, group, unit_id, JUNE),
    ).fetchone()[0]


def codes(conn, sql: str) -> list[str]:
    return sorted(row[0] for row in conn.execute(sql).fetchall())


# --- ведомость: главное, что видит человек на экране -------------------------


def test_manager_sees_payslips_of_own_unit_only(db):
    """Ровно тот дефект, из-за которого заведена задача."""
    make_payslip(db, "ns1", U_NS1, "100.00")
    make_payslip(db, "ns2", U_NS2, "200.00")
    make_payslip(db, "bg1", U_BG1, "300.00")

    with as_app_user(db, USER_MANAGER) as conn:
        seen = codes(conn, """
            select u.code from payslips p join units u on u.id = p.unit_id
        """)
    assert seen == ["NS1"]


def test_director_and_accountant_still_see_every_unit(db):
    """Контроль: без него тест выше был бы зелёным и на пустой выборке."""
    make_payslip(db, "ns1", U_NS1, "100.00")
    make_payslip(db, "ns2", U_NS2, "200.00")
    make_payslip(db, "bg1", U_BG1, "300.00")

    for user in (USER_DIRECTOR, USER_ACCOUNTANT):
        with as_app_user(db, user) as conn:
            seen = codes(conn, """
                select u.code from payslips p join units u on u.id = p.unit_id
            """)
        assert seen == ["BG1", "NS1", "NS2"], user


def test_no_trace_of_other_units_in_the_totals(db):
    """D023: не видно ни строк, ни следа в итогах.

    Итог ведомости складывается из компонентов, поэтому проверяется именно
    сумма компонентов: строку спрятать, а сумму оставить — это тот же показ
    чужих данных, только одним числом.
    """
    make_payslip(db, "ns1", U_NS1, "100.00")
    make_payslip(db, "ns2", U_NS2, "200.00")
    make_payslip(db, "bg1", U_BG1, "300.00")

    with as_app_user(db, USER_MANAGER) as conn:
        total = conn.execute(
            "select coalesce(sum(amount), 0) from pay_components"
        ).fetchone()[0]
        rows = conn.execute("select count(*) from pay_components").fetchone()[0]
    assert (rows, total) == (1, Decimal("100.00"))

    with as_app_user(db, USER_DIRECTOR) as conn:
        total = conn.execute(
            "select coalesce(sum(amount), 0) from pay_components"
        ).fetchone()[0]
    assert total == Decimal("600.00")


# --- остальные таблицы, у которых есть точка ---------------------------------


def test_manager_sees_own_unit_only_in_the_directory(db):
    with as_app_user(db, USER_MANAGER) as conn:
        assert codes(conn, "select code from units") == ["NS1"]
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert codes(conn, "select code from units") == ["BG1", "NS1", "NS2"]


def test_manager_sees_timesheets_of_own_unit_only(db):
    make_timesheet(db, "ts-ns1", U_NS1)
    make_timesheet(db, "ts-ns2", U_NS2)

    with as_app_user(db, USER_MANAGER) as conn:
        assert conn.execute("select count(*) from timesheets").fetchone()[0] == 1
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select count(*) from timesheets").fetchone()[0] == 2


def test_manager_sees_employment_terms_of_own_unit_only(db):
    """Условия найма — это ставка человека. Чужая точка её показывать не должна."""
    make_term(db, "term-ns1", U_NS1)
    make_term(db, "term-bg1", U_BG1)

    with as_app_user(db, USER_MANAGER) as conn:
        assert conn.execute("select count(*) from employment_terms").fetchone()[0] == 1
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select count(*) from employment_terms").fetchone()[0] == 2


# --- решения, зафиксированные явно -------------------------------------------


def test_rows_without_a_unit_stay_visible(db):
    """Строка без точки не принадлежит чужой точке — она не принадлежит никакой.

    Решено показывать: `unit_id` обнуляется при удалении точки
    (`on delete set null`), и молча терять такие строки хуже, чем показать их
    тому, кто и так работает внутри этого партнёра. Зафиксировано тестом, чтобы
    это было решением, а не побочным эффектом записи политики.
    """
    make_payslip(db, "nowhere", None, "50.00")
    make_timesheet(db, "ts-nowhere", None)

    with as_app_user(db, USER_MANAGER) as conn:
        assert conn.execute(
            "select count(*) from payslips where unit_id is null"
        ).fetchone()[0] == 1
        assert conn.execute(
            "select count(*) from timesheets where unit_id is null"
        ).fetchone()[0] == 1


def test_membership_without_units_means_every_unit(db):
    """`unit_ids is null` у директора — это «все точки», а не «ни одной»."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select app_unit_ids(%s) is null", (T1,)).fetchone()[0] is True
    with as_app_user(db, USER_MANAGER) as conn:
        units = conn.execute("select app_unit_ids(%s)", (T1,)).fetchone()[0]
    assert [str(unit) for unit in units] == [U_NS1]


def test_without_context_nothing_is_visible(db):
    """Контекст не выставлен — пусто, а не «все точки»."""
    make_payslip(db, "ns1", U_NS1, "100.00")

    with as_app_user(db, None) as conn:
        assert conn.execute("select count(*) from units").fetchone()[0] == 0
        assert conn.execute("select count(*) from payslips").fetchone()[0] == 0
        assert conn.execute("select count(*) from pay_components").fetchone()[0] == 0


# =============================================================================
# То же самое на экране: страница периода в браузерном клиенте Django
# =============================================================================
# Проверка ролью app_user выше говорит, что база отдаёт правильный срез. Здесь
# проверяется, что человек видит именно его — и что шапка не обещает больше,
# чем показывает страница.


def units_on_the_page(html: str) -> set[str]:
    """Коды точек из колонки «Точка» ведомости."""
    return {
        match.group(1)
        for match in re.finditer(r"<td>([A-Z]{2}\d)</td>", html)
    }


def sheet_rows(html: str) -> int:
    return len(re.findall(r"<td class=\"num strong\">", html))


@pytest.fixture
def calculated(client, web_env):
    """Посчитанный июнь на данных сида: материал для обеих ролей."""
    wipe_payruns(web_env)
    login_as(client, "director")
    client.post(period_url(client) + "calculate/", follow=True)
    return None


def test_manager_page_shows_only_own_unit(client, calculated):
    """Ведомость управляющего — только его точка, и не пустая."""
    login_as(client, "director")
    director = body(client.get(period_url(client)))

    login_as(client, "manager")
    manager = body(client.get(period_url(client)))

    assert len(units_on_the_page(director)) > 1, "у директора должно быть больше одной точки"
    assert units_on_the_page(manager) == {"NS1"}
    assert 0 < sheet_rows(manager) < sheet_rows(director)


def test_header_names_the_unit_it_shows(client, calculated):
    """Обещание в шапке и содержимое страницы — про одно и то же."""
    login_as(client, "manager")
    manager = body(client.get(period_url(client)))
    assert "точка: NS1" in manager

    # Директору точку не подписываем: ограничения нет, объяснять нечего.
    login_as(client, "director")
    assert "точка: " not in body(client.get(period_url(client)))


def amount(text: str) -> Decimal:
    """Сумма со страницы обратно в число: `1 234,50` -> Decimal."""
    return Decimal(text.replace(" ", "").replace(",", "."))


def row_totals(html: str) -> list[Decimal]:
    return [amount(cell) for cell in re.findall(r'<td class="num strong">([^<]+)</td>', html)]


def ledger_totals(html: str) -> dict[str, Decimal]:
    """Таблица «Итоги по регистрам»: регистр -> сумма, без строки «Итого»."""
    tail = html.split("Итоги по регистрам", 1)[-1]
    return {
        title: amount(cell)
        for title, cell in re.findall(r'<tr><td>([^<]+)</td><td class="num">([^<]+)</td></tr>', tail)
        if title != "Итого"
    }


def grand_total(html: str) -> Decimal:
    """Строка «Итого» под таблицей регистров — главное число страницы."""
    tail = html.split("Итоги по регистрам", 1)[-1]
    match = re.search(r'<tr><td>Итого</td><td class="num">([^<]+)</td></tr>', tail)
    assert match, "на странице нет итога по регистрам"
    return amount(match.group(1))


def test_manager_totals_carry_no_trace_of_other_units(client, calculated):
    """D023 на экране: итог посчитан по видимому срезу, а не по всем точкам."""
    login_as(client, "manager")
    manager = body(client.get(period_url(client)))
    login_as(client, "director")
    director = body(client.get(period_url(client)))

    # Итог сходится ровно с показанными строками — значит скрытая точка не
    # осталась в нём слагаемым.
    assert grand_total(manager) == sum(row_totals(manager)) == sum(ledger_totals(manager).values())
    assert grand_total(director) == sum(row_totals(director)) == sum(ledger_totals(director).values())

    # Контроль сравнивает **один и тот же регистр**: общий итог у управляющего
    # меньше и без фильтра по точкам — просто потому, что внутренний регистр ему
    # не виден. Официальный виден обоим, и разойтись он может только по точкам.
    official_manager = ledger_totals(manager)["Официальный"]
    official_director = ledger_totals(director)["Официальный"]
    assert Decimal(0) < official_manager < official_director
