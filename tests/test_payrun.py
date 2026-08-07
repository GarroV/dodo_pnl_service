"""
Расчёт периода из интерфейса и ведомость на экране (T042).

Главное, что здесь проверяется:

1. **Точность.** Числа, попавшие в базу через кнопку в браузере, совпадают с
   прямым вызовом движка на тех же данных. Вход для сверки собирается в тесте
   отдельным кодом, прямо из SQL, — иначе тест проверял бы ту же самую ошибку
   отображения, что и код.
2. **Итог по видимому срезу (D023).** У бухгалтера и у директора ведомость
   разная, и у каждого сумма сходится с показанными строками.
3. **Идемпотентность.** Повторный запуск не плодит ведомости.

Тесты гоняются на живом Postgres с сидом (фикстура `web_env` в conftest); без
Postgres пропускаются вместе с остальными тестами схемы.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from conftest import body, login_as, period_url, wipe_payruns
from payroll import Employee, PayrollEngine, Timesheet, d

JUNE = "2026-06-01"
CENT = Decimal("0.01")


# --- помощники ---------------------------------------------------------------


def calculate(client) -> object:
    """Нажать «посчитать» так же, как это делает человек: со страницы периода."""
    url = period_url(client)
    return client.post(url + "calculate/", follow=True)


def rows_from_db(dsn: str) -> list[tuple]:
    """Все компоненты расчёта, минуя политики: эталон «что вообще посчиталось»."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute(
            """select e.external_id, c.code, c.amount, c.layer
                 from pay_components c
                 join payslips p on p.id = c.payslip_id
                 join employees e on e.id = p.employee_id
                order by e.external_id, c.code"""
        ).fetchall()


def engine_expectation(dsn: str) -> dict[tuple[str, str], Decimal]:
    """Посчитать движком напрямую по тем же данным базы.

    Вход собирается здесь своим кодом — намеренно не через `payrun.calc`:
    сверка имеет смысл, только если две дороги независимы.
    """
    import psycopg

    from payroll import load_preset

    engine = PayrollEngine(load_preset("serbia-2026"))
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """select e.external_id, g.code, g.layer::text,
                      coalesce(t.scheme, g.scheme), t.base_rate, t.coefficient,
                      ts.hours, ts.insured_hours, ts.norm_hours
                 from timesheets ts
                 join employees e on e.id = ts.employee_id
                 join employment_terms t on t.employee_id = e.id
                 join employee_groups g on g.id = t.group_id
                where ts.period = %s""",
            (JUNE,),
        ).fetchall()

    expected: dict[tuple[str, str], Decimal] = {}
    for ext_id, group, layer, scheme, base_rate, coefficient, hours, insured, norm in rows:
        slip = engine.calculate(
            Employee(
                ext_id=ext_id, name=ext_id, group=group, scheme=scheme,
                base_rate=d(base_rate), coefficient=d(coefficient), layer=layer,
            ),
            Timesheet(
                hours={k: d(v) for k, v in hours.items()},
                insured_hours=d(insured), norm_hours=d(norm),
            ),
        )
        for component in slip.components:
            expected[(ext_id, component.code)] = d(component.amount).quantize(CENT)
    return expected


def sheet_numbers(html: str) -> list[Decimal]:
    """Итоги строк ведомости — последняя числовая ячейка каждой строки таблицы."""
    table = re.search(r'<table class="sheet".*?<tbody>(.*?)</tbody>', html, re.S)
    assert table, "на странице нет ведомости"
    result = []
    for row in re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S):
        cells = re.findall(r'<td class="num[^"]*">(.*?)</td>', row, re.S)
        if cells:
            result.append(unmoney(cells[-1]))
    return result


def unmoney(text: str) -> Decimal:
    """`1 234,50` → Decimal. Прочерк и пустая ячейка — ноль."""
    clean = re.sub(r"<[^>]+>", "", text).strip().replace(" ", "").replace(",", ".")
    if clean in ("", "—"):
        return Decimal(0)
    return Decimal(clean)


def grand_total(html: str) -> Decimal:
    footer = re.search(r'<tr class="grand">(.*?)</tr>', html, re.S)
    assert footer, "в ведомости нет итоговой строки"
    return unmoney(re.findall(r'<td class="num[^"]*">(.*?)</td>', footer.group(0), re.S)[-1])


@pytest.fixture
def clean_payruns(web_env):
    """Известное состояние: расчётов нет. Модули делят одну базу."""
    wipe_payruns(web_env)
    return web_env


# --- выбор пресета -----------------------------------------------------------


def test_preset_is_chosen_by_country_and_date():
    """Страна и дата, а не `if country == 'RS'` в коде расчёта."""
    from datetime import date

    from payrun.rules import select_preset

    code, preset = select_preset("RS", date(2026, 6, 1))
    assert code == "serbia-2026"
    assert preset["currency"] == "RSD"


def test_unknown_country_is_refused_with_a_readable_reason():
    from datetime import date

    from payrun.errors import PayrunRefused
    from payrun.rules import select_preset

    with pytest.raises(PayrunRefused) as exc:
        select_preset("ZZ", date(2026, 6, 1))
    assert "ZZ" in str(exc.value)


def test_preset_from_the_future_is_not_used():
    """Правила 2026 года не должны применяться к периоду 2025-го."""
    from datetime import date

    from payrun.errors import PayrunRefused
    from payrun.rules import select_preset

    with pytest.raises(PayrunRefused):
        select_preset("RS", date(2025, 6, 1))


# --- сборка ведомости (чистая функция) ---------------------------------------


def test_sheet_puts_hours_first_and_sums_by_visible_rows():
    from payrun.sheet import Cell, assemble

    sheet = assemble([
        Cell("Петров", "BG1", "white", "meal", "Обед", Decimal("1500.00")),
        Cell("Петров", "BG1", "white", "hours.regular", "Отработанные", Decimal("1000.00")),
        Cell("Иванов", "NS1", "grey", "hours.regular", "Отработанные", Decimal("2000.00")),
    ])
    assert [c.code for c in sheet.columns] == ["hours.regular", "meal"]
    assert [(r.employee, r.ledger) for r in sheet.rows] == [
        ("Иванов", "grey"), ("Петров", "white"),
    ]
    assert sheet.total == Decimal("4500.00")
    assert sheet.ledger_totals == [("white", Decimal("2500.00")), ("grey", Decimal("2000.00"))]
    # Сумма строк равна показанному итогу — то, что бухгалтер проверяет глазами.
    assert sum(r.total for r in sheet.rows) == sheet.total


def test_one_employee_with_two_ledgers_gives_two_rows():
    """D023: регистр — свойство компонента, поэтому человек бывает в двух строках."""
    from payrun.sheet import Cell, assemble

    sheet = assemble([
        Cell("Петров", "BG1", "grey", "hours.regular", "Отработанные", Decimal("1000.00")),
        Cell("Петров", "BG1", "white", "meal", "Обед", Decimal("1500.00")),
    ])
    assert len(sheet.rows) == 2
    assert sheet.employees == 1
    assert [r.ledger for r in sheet.rows] == ["white", "grey"]


# --- расчёт из интерфейса ----------------------------------------------------


def test_calculation_matches_direct_engine_call(client, clean_payruns):
    """Ключевая проверка задачи: кнопка и прямой вызов движка дают одно и то же."""
    login_as(client, "director")
    assert calculate(client).status_code == 200

    stored = {(ext, code): amount for ext, code, amount, _ in rows_from_db(clean_payruns)}
    expected = engine_expectation(clean_payruns)

    assert stored, "расчёт не записал ни одного компонента"
    assert set(stored) == set(expected)
    mismatch = {k: (stored[k], expected[k]) for k in expected if stored[k] != expected[k]}
    assert not mismatch, f"суммы разошлись с движком: {mismatch}"


def test_calculation_covers_every_employee_of_the_period(client, clean_payruns):
    """32 табеля — 32 ведомости. Молчаливый пропуск сотрудника недопустим."""
    import psycopg

    login_as(client, "director")
    calculate(client)

    with psycopg.connect(clean_payruns) as conn:
        slips = conn.execute("select count(*) from payslips").fetchone()[0]
        sheets = conn.execute("select count(*) from timesheets").fetchone()[0]
        schemes = conn.execute(
            """select count(distinct coalesce(t.scheme, g.scheme))
                 from employment_terms t join employee_groups g on g.id = t.group_id"""
        ).fetchone()[0]
    assert slips == sheets == 32
    assert schemes == 4, "в сиде должны быть все четыре схемы расчёта"


def test_recalculation_does_not_duplicate_anything(client, clean_payruns):
    """Идемпотентность из DoD блока: второй запуск не плодит ведомости."""
    import psycopg

    login_as(client, "director")
    calculate(client)
    first = rows_from_db(clean_payruns)
    calculate(client)
    second = rows_from_db(clean_payruns)

    assert first == second
    with psycopg.connect(clean_payruns) as conn:
        assert conn.execute("select count(*) from payruns").fetchone()[0] == 1
        assert conn.execute("select count(*) from payslips").fetchone()[0] == 32


def test_employee_without_terms_is_reported_not_skipped_silently(client, clean_payruns):
    """Нет условий найма — расчёт отказывается, а не считает молча без человека."""
    import psycopg

    with psycopg.connect(clean_payruns, autocommit=True) as conn:
        tenant, unit = conn.execute(
            """select t.id, (select id from units where tenant_id = t.id order by code limit 1)
                 from tenants t where t.code = 'rs-dev'"""
        ).fetchone()
        employee = conn.execute(
            """insert into employees (tenant_id, external_id, first_name, last_name)
               values (%s, 'no-terms', 'Без', 'Условий') returning id""",
            (tenant,),
        ).fetchone()[0]
        conn.execute(
            """insert into timesheets (tenant_id, employee_id, unit_id, period,
                                       insured_hours, norm_hours, hours)
               values (%s, %s, %s, %s, 176, 176, '{"regular": "176"}')""",
            (tenant, employee, unit, JUNE),
        )
    try:
        login_as(client, "director")
        response = calculate(client)
        assert response.status_code == 409
        text = body(response)
        assert "no-terms" in text
        with psycopg.connect(clean_payruns) as conn:
            assert conn.execute("select count(*) from payslips").fetchone()[0] == 0
    finally:
        with psycopg.connect(clean_payruns, autocommit=True) as conn:
            conn.execute("delete from timesheets where employee_id = %s", (employee,))
            conn.execute("delete from employees where id = %s", (employee,))


# --- кто может считать -------------------------------------------------------


def test_accountant_cannot_launch_calculation(client, clean_payruns):
    """Расчёт пишет строки во все регистры; роль, которая их не видит, не пишет.

    Иначе `insert ... returning` упёрся бы в политику базы и человек получил бы
    500-ю вместо объяснения.
    """
    import psycopg

    login_as(client, "accountant")
    response = calculate(client)
    assert response.status_code == 403
    assert "регистр" in body(response).lower()

    with psycopg.connect(clean_payruns) as conn:
        assert conn.execute("select count(*) from payslips").fetchone()[0] == 0


def test_calculation_without_login_writes_nothing(client, clean_payruns):
    import psycopg

    login_as(client, "director")
    url = period_url(client)
    client.post("/dev/logout/")

    assert client.post(url + "calculate/").status_code == 404
    with psycopg.connect(clean_payruns) as conn:
        assert conn.execute("select count(*) from payruns").fetchone()[0] == 0


def test_database_refuses_a_component_of_an_invisible_ledger(clean_payruns):
    """Страховка: даже если проверка в коде исчезнет, база не даст записать.

    Именно это и делает отказ выше не декоративным.
    """
    import psycopg

    accountant = "0f1efdd9-bc29-5eae-8b9a-3ab006d71d44"  # видит только официальный
    with psycopg.connect(clean_payruns) as conn:
        tenant, employee = conn.execute(
            """select t.id, (select id from employees where tenant_id = t.id limit 1)
                 from tenants t where t.code = 'rs-dev'"""
        ).fetchone()
        # Ролью приложения, как ходит сервис: на владельца таблиц политики
        # не действуют, и проверка была бы фиктивной.
        conn.execute("set local role app_user")
        conn.execute("select set_config('app.user_id', %s, true)", (accountant,))
        payrun = conn.execute(
            "insert into payruns (tenant_id, period) values (%s, %s) returning id",
            (tenant, JUNE),
        ).fetchone()[0]
        payslip = conn.execute(
            """insert into payslips (tenant_id, payrun_id, employee_id)
               values (%s, %s, %s) returning id""",
            (tenant, payrun, employee),
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.Error):
            conn.execute(
                """insert into pay_components
                       (tenant_id, payslip_id, code, title, amount, layer)
                   values (%s, %s, 'hours.regular', 'Часы', 100, 'grey') returning id""",
                (tenant, payslip),
            )
        # После отказа базы транзакция аварийная — откатываем целиком.
        conn.rollback()


# --- ведомость на экране -----------------------------------------------------


def test_sheet_shows_a_row_per_employee_and_ledger(client, clean_payruns):
    login_as(client, "director")
    calculate(client)
    html = body(client.get(period_url(client)))

    assert "Ведомость" in html
    # Кухня даёт часы в дополнительном регистре и надбавку в официальном:
    # у одного человека две строки, поэтому строк больше, чем сотрудников.
    assert len(sheet_numbers(html)) > 32


def test_totals_match_the_rows_the_role_can_see(client, clean_payruns):
    """D023 целиком: у двух ролей разные строки и разные сходящиеся итоги."""
    login_as(client, "director")
    calculate(client)
    director = body(client.get(period_url(client)))

    login_as(client, "accountant")
    accountant = body(client.get(period_url(client)))

    director_rows, accountant_rows = sheet_numbers(director), sheet_numbers(accountant)
    assert len(accountant_rows) < len(director_rows)
    assert sum(director_rows) == grand_total(director)
    assert sum(accountant_rows) == grand_total(accountant)
    assert grand_total(accountant) < grand_total(director)


def test_supplementary_ledger_leaves_no_trace_for_the_accountant(client, clean_payruns):
    """Ни строк, ни следа в итогах: разницу нельзя получить вычитанием."""
    import psycopg

    from web.format import money

    login_as(client, "director")
    calculate(client)

    director = body(client.get(period_url(client)))
    with psycopg.connect(clean_payruns) as conn:
        white, grey = conn.execute(
            """select sum(amount) filter (where layer = 'white'),
                      sum(amount) filter (where layer = 'grey')
                 from pay_components"""
        ).fetchone()
    assert grey > 0, "в сиде нет дополнительного регистра — проверять нечего"

    login_as(client, "accountant")
    accountant = body(client.get(period_url(client)))

    # Ни одной строки чужого регистра и ни следа его суммы.
    assert "Дополнительный" in director and "Дополнительный" not in accountant
    assert money(grey) not in accountant
    # Итог бухгалтера — ровно официальный регистр: разницу нельзя получить
    # вычитанием, потому что общей суммы он нигде не видит.
    assert grand_total(accountant) == white
    assert grand_total(director) == white + grey
    # Суммарные поля ведомости (нето, бруто, взносы) не показываются вовсе:
    # политики видимости регистров на них нет, и они выдали бы скрытое.
    assert "Нето" not in accountant and "Бруто" not in accountant


def test_period_page_without_calculation_offers_to_run_it(client, clean_payruns):
    login_as(client, "director")
    html = body(client.get(period_url(client)))
    assert "посчитать" in html.lower()
    assert "Расчёта за этот период ещё не было" in html
