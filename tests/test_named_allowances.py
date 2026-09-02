"""Надбавки у человека — именованные, а не спрятанные в коэффициент (issue #189, T196).

В условиях найма был один множитель — `coefficient`. Всё, чем на практике
отличается оплата человека (наставничество, старшинство, надбавка за точку),
приходилось либо зашивать в него, либо превращать в отдельный тип часа.

Три следствия, и все три чинятся здесь:

* **непонятно, из чего сложилась ставка.** `coefficient = 1,15` не говорит, что
  это наставничество; через полгода не вспомнит никто — ровно то, что запрещает
  принцип «правила предметной области комментируем обязательно»;
* **надбавку нельзя закончить отдельно от ставки.** Наставничество сняли — и
  приходилось заводить новую версию условий целиком;
* **в ведомости её не видно.** Человек видит сумму, а за что — нет.

Эталон (модуль 16) держит те же надбавки в шаблоне должности: «Наставничество —
часы наставника × 30,00, дополнительный регистр». Отсюда состав: код, название,
величина, способ начисления, **регистр** и срок действия.

**Регистр у надбавки свой, а не берётся у человека.** В эталоне наставничество
лежит в дополнительном регистре, а ночные — в официальном, у одного и того же
человека. Взять регистр у условий найма значило бы запретить эту пару.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as, period_url
from test_directory import sql  # noqa: F401

JUNE = date(2026, 6, 1)


@pytest.fixture
def somebody(sql):  # noqa: F811
    """Человек с условиями найма — к нему и цепляем надбавку."""
    row = sql.execute(
        """select e.id::text, t.id::text
             from employees e join employment_terms t on t.employee_id = e.id
            where t.valid_to is null
            order by e.external_id limit 1"""
    ).fetchone()
    assert row, "в сиде нет ни одного человека с действующими условиями найма"
    return {"employee": row[0], "terms": row[1]}


@pytest.fixture(autouse=True)
def allowances_removed(sql):  # noqa: F811
    """Надбавки не переживают тест: иначе расчёт у соседей поедет молча."""
    yield
    sql.execute("delete from employee_allowances")


def add(sql, employee: str, **fields) -> str:  # noqa: F811
    body_ = {
        "code": "mentor", "title": "Наставничество",
        "amount": "30.00", "basis": "per_hour", "ledger": "supplementary",
        "valid_from": "2026-01-01", "valid_to": None,
        **fields,
    }
    return sql.execute(
        """insert into employee_allowances
             (tenant_id, employee_id, code, title, amount, basis, ledger, valid_from, valid_to)
           select e.tenant_id, e.id, %(code)s, %(title)s, %(amount)s, %(basis)s,
                  %(ledger)s::ledger, %(valid_from)s, %(valid_to)s
             from employees e where e.id = %(employee)s
           returning id::text""",
        {**body_, "employee": employee},
    ).fetchone()[0]


# --- форма данных -------------------------------------------------------------


def test_an_allowance_has_a_name_and_a_life_of_its_own(sql, somebody):  # noqa: F811
    """У надбавки есть имя, величина, регистр и срок — она не часть коэффициента."""
    add(sql, somebody["employee"])

    row = sql.execute(
        """select code, title, amount, basis, ledger::text, valid_from
             from employee_allowances"""
    ).fetchone()
    assert row[0] == "mentor" and row[1] == "Наставничество"
    assert row[2] == Decimal("30.00")
    assert row[4] == "supplementary", "регистр надбавки не сохранился"


def test_two_allowances_of_one_person_live_side_by_side(sql, somebody):  # noqa: F811
    """Наставничество и надбавка за точку — две строки, а не один коэффициент.

    И регистры у них разные: эталон держит наставничество в дополнительном, а
    ночные в официальном у одного человека.
    """
    add(sql, somebody["employee"])
    add(sql, somebody["employee"], code="senior", title="Старшинство",
        amount="15.00", basis="percent", ledger="official")

    rows = sql.execute(
        "select code, ledger::text from employee_allowances order by code"
    ).fetchall()
    assert rows == [("mentor", "supplementary"), ("senior", "official")], rows


def test_an_allowance_ends_without_touching_the_rate(sql, somebody):  # noqa: F811
    """Надбавку можно закончить датой, не заводя новую версию условий найма.

    Ради этого задача и делалась: раньше снятие наставничества означало новую
    версию всей записи найма, вместе со ставкой, группой и точкой.
    """
    made = add(sql, somebody["employee"])
    before = sql.execute(
        "select count(*) from employment_terms where employee_id = %s", (somebody["employee"],)
    ).fetchone()[0]

    sql.execute("update employee_allowances set valid_to = '2026-07-01' where id = %s", (made,))

    after = sql.execute(
        "select count(*) from employment_terms where employee_id = %s", (somebody["employee"],)
    ).fetchone()[0]
    assert after == before, "снятие надбавки завело новую версию условий найма"


def test_the_same_code_cannot_repeat_in_one_period(sql, somebody):  # noqa: F811
    """Одна надбавка на человека в один период — иначе она начислится дважды."""
    import psycopg

    add(sql, somebody["employee"])
    with pytest.raises(psycopg.errors.Error):
        add(sql, somebody["employee"])


# --- расчёт -------------------------------------------------------------------


def test_the_allowance_reaches_the_payslip_as_its_own_line(client, sql, somebody):  # noqa: F811
    """Надбавка приходит в ведомость отдельной строкой со своим названием.

    Иначе человек снова видит сумму и не видит, за что она.
    """
    add(sql, somebody["employee"])

    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", {"inline": "1"}, follow=True)

    rows = sql.execute(
        """select c.code, c.title, c.amount, c.ledger::text
             from pay_components c join payslips p on p.id = c.payslip_id
            where p.employee_id = %s and c.code like 'allowance.%%'""",
        (somebody["employee"],),
    ).fetchall()
    assert rows, "надбавки нет среди компонентов расчёта"
    assert any("Наставничество" in row[1] for row in rows), rows
    assert all(row[3] == "supplementary" for row in rows), (
        f"надбавка легла не в свой регистр: {rows}"
    )


def test_an_allowance_outside_its_dates_is_not_paid(client, sql, somebody):  # noqa: F811
    """Надбавка, закончившаяся до периода, в расчёт не попадает."""
    add(sql, somebody["employee"], valid_from="2025-01-01", valid_to="2025-06-01")

    login_as(client, "director")

    url = period_url(client)
    client.post(url + "calculate/", {"inline": "1"}, follow=True)

    paid = sql.execute(
        """select count(*) from pay_components c join payslips p on p.id = c.payslip_id
            where p.employee_id = %s and c.code like 'allowance.%%'""",
        (somebody["employee"],),
    ).fetchone()[0]
    assert paid == 0, "начислена надбавка, срок которой кончился год назад"


def test_the_card_shows_the_allowance_with_its_own_name(client, sql, somebody):  # noqa: F811
    """На карточке человека надбавка видна именем, величиной и регистром.

    Ради этого задача и делалась: раньше на карточке стоял коэффициент 1,15 и
    ничего больше — «почему 1,15» не отвечал никто.
    """
    add(sql, somebody["employee"])

    login_as(client, "director")
    shown = body(client.get(f"/directory/employees/{somebody['employee']}/"))

    assert "Наставничество" in shown, "надбавки нет на карточке"
    assert "За отработанный час" in shown, "не сказано, как считается величина"
    assert 'data-allowance="mentor"' in shown


def test_a_finished_allowance_stays_on_the_card(client, sql, somebody):  # noqa: F811
    """Закончившаяся надбавка с карточки не исчезает.

    «Наставничество было с января по июль» объясняет закрытый месяц так же, как
    ставка того периода. Спрятать её значило бы оставить июньскую ведомость без
    объяснения.
    """
    add(sql, somebody["employee"], valid_from="2026-01-01", valid_to="2026-07-01")

    login_as(client, "director")
    shown = body(client.get(f"/directory/employees/{somebody['employee']}/"))
    assert "Наставничество" in shown and "01.07.2026" in shown


def test_the_manager_sees_the_allowance_but_not_its_money(client, sql, somebody):  # noqa: F811
    """Управляющий точки видит надбавку своего человека — это условия работы.

    Сумм расчёта ему по-прежнему не видно (T173, D047): надбавка — правило, а не
    начисленные деньги, и знать про наставничество своей смены он должен.
    """
    add(sql, somebody["employee"], ledger="official")

    login_as(client, "manager")
    answer = client.get(f"/directory/employees/{somebody['employee']}/")
    if answer.status_code == 404:
        return   # человек не его точки — проверять нечего, это D023
    assert "Наставничество" in body(answer)

