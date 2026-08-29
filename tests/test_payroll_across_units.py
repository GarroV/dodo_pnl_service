"""Затраты человека делятся по его точкам, а не падают на одну (D055, #194, T197).

Решение владельца 27.08.2026, дословно: «управляющий может быть на несколько
пиццерий один… чтобы условный человек в Нови-Саде делился только по пиццериям
Нови-Сада, а в Белграде только на Белград», и про офис: «равномерно должно,
потому что офис на всех работает, вне зависимости».

Что было. У ведомости одна точка (`payslips.unit_id`), и перенос в P&L (T208)
клал весь ФОТ человека на неё. Для пиццамейкера это правда, для управляющего
двумя точками — нет: его зарплата целиком ложилась в затраты одной пиццерии, и
её P&L выглядел хуже соседнего без всякой причины.

Что теперь. У человека может быть **несколько** точек; его деньги делятся между
ними. Точек нет вовсе — это офис: он работает на всю сеть, и такая строка уходит
на разнесение общим правилом, как любой расход юрлица.

**Умолчание — поровну**, как и сказал владелец. Способ деления — настройка, но
пока в продукте один способ; второй («по выручке») уже существует у расходов и
подключается тем же полем, когда выручка появится.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import login_as
from test_closing_readiness import calculated  # noqa: F401
from test_directory import sql  # noqa: F401


@pytest.fixture(autouse=True)
def units_of_people_removed(sql):  # noqa: F811
    """Привязки к точкам не переживают тест.

    Без этой уборки они остаются в базе и меняют расчёт у **соседних** файлов:
    ведомость начинает делиться по точкам там, где тест этого не заводил, и
    сорок девять проверок расчёта падают, не понимая почему. Поймано полным
    прогоном: по отдельности каждый файл зелёный, вместе — нет.
    """
    yield
    sql.execute("delete from employee_units")


def approve(client, url):
    return client.post(url + "approve/", {"postpone_blockers": "1"}, follow=True)


def payroll_by_unit(sql) -> dict:  # noqa: F811
    rows = sql.execute(
        """select coalesce(u.code, 'сеть'), sum(f.amount)
             from facts f left join units u on u.id = f.unit_id
            where f.dedup_key like 'payrun:%%' and f.superseded_at is null
              and f.allocation <> 'split'
            group by 1"""
    ).fetchall()
    return {code: amount for code, amount in rows}


def put_on_units(sql, employee_key: str, codes: list[str]) -> None:  # noqa: F811
    """Привязать человека к точкам — так же, как это сделает экран кадров."""
    sql.execute(
        """delete from employee_units
            where employee_id = (select id from employees where external_id = %s)""",
        (employee_key,),
    )
    for code in codes:
        sql.execute(
            """insert into employee_units (tenant_id, employee_id, unit_id, valid_from)
               select e.tenant_id, e.id, u.id, '2020-01-01'
                 from employees e, units u
                where e.external_id = %s and u.code = %s""",
            (employee_key, code),
        )


def somebody(sql) -> str:  # noqa: F811
    return sql.execute(
        """select e.external_id from employees e
             join payslips p on p.employee_id = e.id
            order by e.external_id limit 1"""
    ).fetchone()[0]


# --- ядро ---------------------------------------------------------------------


def test_a_person_on_two_units_is_split_between_them(client, sql, calculated):  # noqa: F811
    """Человек на двух точках — его деньги делятся между ними."""
    who = somebody(sql)
    put_on_units(sql, who, ["BG1", "NS1"])

    login_as(client, "director")
    approve(client, calculated)

    by_unit = payroll_by_unit(sql)
    assert by_unit.get("BG1") and by_unit.get("NS1"), (
        f"деньги человека не разошлись по его точкам: {by_unit}"
    )


def test_the_split_keeps_the_total(client, sql, calculated):  # noqa: F811
    """Сумма по точкам равна полной стоимости — деление не теряет копеек."""
    put_on_units(sql, somebody(sql), ["BG1", "NS1", "NS2"])

    login_as(client, "director")
    approve(client, calculated)

    in_facts = -sum(payroll_by_unit(sql).values(), Decimal("0"))
    in_payslips = sql.execute(
        """select coalesce(sum(t.total_cost), 0) from payslip_totals t
             join payslips p on p.id = t.payslip_id
             join payruns r on r.id = p.payrun_id
            where r.period = '2026-06-01'"""
    ).fetchone()[0]
    assert in_facts == in_payslips, f"после деления {in_facts} против {in_payslips}"


def test_a_person_without_units_goes_to_the_whole_network(client, sql, calculated):  # noqa: F811
    """Точек нет — это офис: строка уходит на разнесение общим правилом.

    Владелец: «офис на всех работает, вне зависимости». Класть его на точку, где
    человек случайно числится, значило бы ухудшать её P&L без причины.
    """
    who = somebody(sql)
    put_on_units(sql, who, [])
    sql.execute(
        """update payslips set unit_id = null
            where employee_id = (select id from employees where external_id = %s)""",
        (who,),
    )

    login_as(client, "director")
    approve(client, calculated)

    network = sql.execute(
        """select count(*) from facts
            where dedup_key like 'payrun:%%' and superseded_at is null
              and (unit_id is null or allocation = 'allocated')"""
    ).fetchone()[0]
    assert network, "ФОТ без точки никуда не делся из одной точки"


def test_the_units_of_a_person_are_versioned(sql, web_env):  # noqa: F811
    """Привязка к точкам живёт с датами: перевод человека не переписывает прошлое.

    Тот же довод, что у условий найма (D020): закрытый месяц обязан считаться
    теми точками, которые были у человека тогда.
    """
    columns = {
        row[0] for row in sql.execute(
            "select column_name from information_schema.columns "
            "where table_name = 'employee_units'"
        ).fetchall()
    }
    assert {"valid_from", "valid_to"} <= columns, (
        f"привязка к точкам не версионируется: {sorted(columns)}"
    )
