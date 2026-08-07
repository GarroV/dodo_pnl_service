"""Версионирование правил по датам: периоды действия не пересекаются.

Зачем это в базе, а не в коде (D015). Правила версионируются по датам — ставки,
условия найма, разнесение расходов. Если у одного сотрудника окажутся две
одновременно действующие версии условий, расчёт возьмёт какую-то одну, и месяц
будет посчитан правдоподобно, но не тем правилом. Такая ошибка не падает: она
даёт неверное число и молчит. Поэтому инвариант держит база — ограничение
`EXCLUDE` поверх `btree_gist`.

Границы периода — `[valid_from, valid_to)`: конец не включается. Так же читает
код расчёта (`.exclude(valid_to__lte=period)`), и так стыкующиеся версии
(«с 1 июня по 1 июля» и «с 1 июля») не считаются пересечением.
"""
from __future__ import annotations

import pytest

from conftest import CP_EPS, T1, T2, as_app_user

pytestmark = pytest.mark.usefixtures("db")

JAN = "2026-01-01"
APR = "2026-04-01"
JUL = "2026-07-01"
OCT = "2026-10-01"


# --- вспомогательное ---------------------------------------------------------


def _employee(conn, ext_id: str = "overlap-1", tenant: str = T1) -> str:
    return conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Тестов') returning id""",
        (tenant, ext_id),
    ).fetchone()[0]


def _group(conn, code: str = "overlap", tenant: str = T1) -> str:
    return conn.execute(
        """insert into employee_groups (tenant_id, code, title, scheme, ledger)
           values (%s, %s, 'Группа', 'standard', 'official') returning id""",
        (tenant, code),
    ).fetchone()[0]


def _term(conn, employee: str, group: str, valid_from: str, valid_to: str | None,
          tenant: str = T1) -> None:
    conn.execute(
        """insert into employment_terms
               (tenant_id, employee_id, group_id, base_rate, valid_from, valid_to)
           values (%s, %s, %s, 300, %s, %s)""",
        (tenant, employee, group, valid_from, valid_to),
    )


def _override(conn, path: str, valid_from: str, valid_to: str | None,
              scope_id: str | None = None, scope_type: str = "country") -> None:
    conn.execute(
        """insert into rule_overrides
               (tenant_id, scope_type, scope_id, path, value, valid_from, valid_to)
           values (%s, %s, %s, %s, '1.0'::jsonb, %s, %s)""",
        (T1, scope_type, scope_id, path, valid_from, valid_to),
    )


def _rejected(conn):
    """Отказ базы по пересечению периодов, не по чему-то другому."""
    import psycopg

    return pytest.raises(psycopg.errors.ExclusionViolation)


# --- условия найма -----------------------------------------------------------


def test_overlapping_employment_terms_are_rejected(db):
    """Две версии условий на один и тот же месяц — отказ базы."""
    employee, group = _employee(db), _group(db)
    _term(db, employee, group, JAN, OCT)
    with _rejected(db), db.transaction():
        _term(db, employee, group, APR, None)


def test_adjacent_employment_terms_are_allowed(db):
    """Стык не пересечение: конец периода не входит в него.

    Перевод сотрудника оформляется именно так — «по 1 июля» и «с 1 июля».
    """
    employee, group = _employee(db), _group(db)
    _term(db, employee, group, JAN, JUL)
    _term(db, employee, group, JUL, None)
    count = db.execute(
        "select count(*) from employment_terms where employee_id = %s", (employee,)
    ).fetchone()[0]
    assert count == 2


def test_open_ended_term_blocks_later_versions(db):
    """Версия без конца действует до бесконечности и перекрывает всё после себя."""
    employee, group = _employee(db), _group(db)
    _term(db, employee, group, JAN, None)
    with _rejected(db), db.transaction():
        _term(db, employee, group, OCT, None)


def test_other_employee_is_not_affected(db):
    """Ограничение по сотруднику, а не по таблице целиком."""
    group = _group(db)
    first, second = _employee(db, "overlap-a"), _employee(db, "overlap-b")
    _term(db, first, group, JAN, None)
    _term(db, second, group, JAN, None)  # не должно упасть


def test_same_employee_in_another_tenant_is_not_affected(db):
    """Ключ включает тенанта: чужой партнёр не может мешать своими правилами."""
    group_one, group_two = _group(db, "overlap-1"), _group(db, "overlap-2", tenant=T2)
    first = _employee(db, "overlap-t1")
    second = _employee(db, "overlap-t2", tenant=T2)
    _term(db, first, group_one, JAN, None)
    _term(db, second, group_two, JAN, None, tenant=T2)


# --- переопределения правил --------------------------------------------------


def test_overlapping_overrides_on_the_same_path_are_rejected(db):
    """Два значения одного правила на одну дату — расчёт взял бы случайное."""
    _override(db, "rates.income_tax", JAN, OCT)
    with _rejected(db), db.transaction():
        _override(db, "rates.income_tax", APR, None)


def test_overrides_on_different_paths_do_not_collide(db):
    _override(db, "rates.income_tax", JAN, None)
    _override(db, "rates.employer_contributions", JAN, None)


def test_overlap_is_caught_even_when_scope_id_is_null(db):
    """Ловушка: `scope_id` пуст у страновых и партнёрских переопределений.

    В ограничении `EXCLUDE` сравнение идёт оператором `=`, а `null = null` даёт
    null, то есть «не совпало». Без приведения пустого scope_id к константе
    ограничение молча не защищало бы ровно тот уровень, который используется
    чаще всего.
    """
    _override(db, "hour_types.night.pay_percent", JAN, None, scope_id=None)
    with _rejected(db), db.transaction():
        _override(db, "hour_types.night.pay_percent", APR, None, scope_id=None)


def test_same_path_in_different_scopes_is_allowed(db):
    """Слои переопределений на то и слои: страна и группа спорят по правилам."""
    group = _group(db, "scoped")
    _override(db, "rates.income_tax", JAN, None, scope_id=None, scope_type="country")
    _override(db, "rates.income_tax", JAN, None, scope_id=group, scope_type="group")


# --- разнесение расходов -----------------------------------------------------


def test_overlapping_allocation_rules_are_rejected(db):
    """Одному контрагенту — одно действующее правило разнесения."""
    item = db.execute("select id from pnl_items limit 1").fetchone()[0]
    for valid_from, valid_to in ((JAN, OCT),):
        db.execute(
            """insert into allocation_rules
                   (tenant_id, counterparty_id, pnl_item_id, method, valid_from, valid_to)
               values (%s, %s, %s, 'even', %s, %s)""",
            (T1, CP_EPS, item, valid_from, valid_to),
        )
    with _rejected(db), db.transaction():
        db.execute(
            """insert into allocation_rules
                   (tenant_id, counterparty_id, pnl_item_id, method, valid_from)
               values (%s, %s, %s, 'even', %s)""",
            (T1, CP_EPS, item, APR),
        )


def test_allocation_rules_of_different_ledgers_coexist(db):
    """Один поставщик может оплачиваться и официально, и из кассы.

    Это разные строки P&L, а не спорные версии одного правила, поэтому регистр
    входит в ключ ограничения. Следствие для блока разнесения: искать правило
    нужно по паре «контрагент + регистр», а не по одному контрагенту.
    """
    item = db.execute("select id from pnl_items limit 1").fetchone()[0]
    for ledger in ("official", "internal"):
        db.execute(
            """insert into allocation_rules
                   (tenant_id, counterparty_id, pnl_item_id, method, ledger, valid_from)
               values (%s, %s, %s, 'even', %s, %s)""",
            (T1, CP_EPS, item, ledger, JAN),
        )


# --- пресеты правил ----------------------------------------------------------


def test_overlapping_presets_are_rejected(db):
    """Пресет страны на дату обязан собираться однозначно."""
    db.execute(
        """insert into rule_presets (code, title, country_code, body, valid_from, valid_to)
           values ('rs-test', 'Тест', 'RS', '{}'::jsonb, %s, %s)""",
        (JAN, OCT),
    )
    with _rejected(db), db.transaction():
        db.execute(
            """insert into rule_presets (code, title, country_code, body, valid_from)
               values ('rs-test', 'Тест 2', 'RS', '{}'::jsonb, %s)""",
            (APR,),
        )


# --- проверка ролью приложения ----------------------------------------------


def test_constraint_holds_for_the_application_role(db):
    """Ограничение — свойство таблицы, а не привилегия: `app_user` тоже не обойдёт.

    Проверяется отдельно, потому что политики RLS ведут себя для разных ролей
    по-разному, и легко решить, что и ограничения тоже.
    """
    from conftest import USER_DIRECTOR

    employee, group = _employee(db), _group(db)
    with as_app_user(db, USER_DIRECTOR) as conn:
        _term(conn, employee, group, JAN, None)
        with _rejected(conn), conn.transaction():
            _term(conn, employee, group, APR, None)
