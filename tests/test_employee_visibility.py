"""Управляющий точки видит только своих людей (T057).

Суть дефекта. Политики видимости точек стоят на `units`, `timesheets`,
`employment_terms`, `payslips`, `pay_components` — и не стоят на `employees`.
Проверено ролью `app_user` под управляющим: справочник сотрудников отдавал всю
сеть вместе с `external_id`, а это национальный идентификатор (в Сербии JMBG),
то есть чувствительные персональные данные. На сегодняшних экранах утечки не
видно — список строится от табеля, — но обещание спеки («видит только своих
людей») держится на том, что режет **база**, а не на том, что ни один экран пока
не спросил лишнего.

Тонкость задачи: точка у сотрудника не хранится. Она в условиях найма, а те
версионируются по датам. Значит «свои люди» надо было определить, и решение
здесь такое:

**Свой человек — тот, у кого есть хоть одна версия условий найма на видимой
точке, независимо от дат.** Каждый случай закреплён отдельным тестом ниже:

* *уволенный* (версия закрыта `valid_to`) остаётся виден своей точке — иначе
  закрытый период перестал бы читаться: его ведомость и табель никуда не делись,
  а имя к ним пропало бы;
* *переведённый в середине месяца* виден обеим точкам — у него две версии, и
  каждая из них настоящая. Ведомость всё равно приходит одной строкой с одной
  точкой (`payslips_payrun_employee_uniq`), так что лишних сумм это не открывает;
* *человек без условий найма вовсе* виден только тем, у кого нет ограничения по
  точкам. Он не «ничей общий», как строка с пустым `unit_id` в миграции `0011`:
  там точка была и пропала (`on delete set null`), а здесь её не назначали.
  Показывать управляющему людей, которых к его точке никто не приписывал, — это
  и есть та самая утечка справочника, ради которой заведена задача.

Проверки идут **ролью `app_user`**: владелец таблиц политики обходит, а
суперпользователь обходит даже `force row level security`.
"""
from __future__ import annotations

import pytest

from conftest import (
    JULY,
    JUNE,
    T1,
    U_BG1,
    U_NS1,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
)


def make_employee(conn, ext_id: str) -> str:
    return conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', %s) returning id""",
        (T1, ext_id, ext_id),
    ).fetchone()[0]


def group(conn) -> str:
    return conn.execute(
        """insert into employee_groups (tenant_id, code, title, scheme)
           values (%s, 'g-emp', 'Группа', 'hourly')
           on conflict (tenant_id, code) do update set title = excluded.title
           returning id""",
        (T1,),
    ).fetchone()[0]


def add_term(conn, employee: str, unit_id: str | None, *, since=JUNE, until=None) -> str:
    return conn.execute(
        """insert into employment_terms
               (tenant_id, employee_id, group_id, unit_id, base_rate, valid_from, valid_to)
           values (%s, %s, %s, %s, 100, %s, %s) returning id""",
        (T1, employee, group(conn), unit_id, since, until),
    ).fetchone()[0]


def hire(conn, ext_id: str, unit_id: str | None, **kwargs) -> str:
    employee = make_employee(conn, ext_id)
    add_term(conn, employee, unit_id, **kwargs)
    return employee


def names(conn) -> list[str]:
    return sorted(row[0] for row in conn.execute("select last_name from employees").fetchall())


@pytest.fixture
def people(db):
    """Пять случаев, ради которых задача и заведена."""
    hire(db, "ns1", U_NS1)
    hire(db, "bg1", U_BG1)
    hire(db, "fired-ns1", U_NS1, since="2026-01-01", until=JUNE)  # уволен, точка NS1
    moved = hire(db, "moved", U_NS1, since="2026-01-01", until=JUNE)
    add_term(db, moved, U_BG1, since=JUNE)  # переведён на другую точку
    make_employee(db, "no-terms")  # заведён и не приписан никуда
    return db


# --- главное требование ------------------------------------------------------


def test_manager_sees_only_people_of_own_unit(people):
    """Ровно тот дефект, из-за которого заведена задача."""
    with as_app_user(people, USER_MANAGER) as conn:
        assert names(conn) == ["fired-ns1", "moved", "ns1"]


def test_director_and_accountant_still_see_everyone(people):
    """Контроль: без него тест выше был бы зелёным и на пустой выборке."""
    for user in (USER_DIRECTOR, USER_ACCOUNTANT):
        with as_app_user(people, user) as conn:
            assert names(conn) == ["bg1", "fired-ns1", "moved", "no-terms", "ns1"], user


def test_national_id_of_another_unit_is_not_readable(people):
    """`external_id` — это JMBG. Чужой не должен приходить ни в каком запросе."""
    with as_app_user(people, USER_MANAGER) as conn:
        found = conn.execute(
            "select count(*) from employees where external_id in ('bg1', 'no-terms')"
        ).fetchone()[0]
    assert found == 0


# --- решения, зафиксированные отдельно ---------------------------------------


def test_a_dismissed_person_stays_visible_to_their_unit(people):
    """Закрытая версия условий найма — всё ещё привязка к точке.

    Иначе закрытый период перестал бы читаться: ведомость и табель на месте, а
    имени к ним нет.
    """
    with as_app_user(people, USER_MANAGER) as conn:
        assert conn.execute(
            "select count(*) from employees where external_id = 'fired-ns1'"
        ).fetchone()[0] == 1


def test_a_transferred_person_is_visible_to_both_units(people):
    """Перевод в середине месяца: обе версии настоящие, обе точки — свои."""
    for user, expected in ((USER_MANAGER, 1), (USER_DIRECTOR, 1)):
        with as_app_user(people, user) as conn:
            assert conn.execute(
                "select count(*) from employees where external_id = 'moved'"
            ).fetchone()[0] == expected, user


def test_a_person_without_terms_is_not_anybodys(people):
    """Человек, не приписанный ни к какой точке, управляющему не показывается."""
    with as_app_user(people, USER_MANAGER) as conn:
        assert conn.execute(
            "select count(*) from employees where external_id = 'no-terms'"
        ).fetchone()[0] == 0
    with as_app_user(people, USER_DIRECTOR) as conn:
        assert conn.execute(
            "select count(*) from employees where external_id = 'no-terms'"
        ).fetchone()[0] == 1


def test_a_manager_cannot_write_a_person_of_another_unit(people):
    """Видимость и запись — одно правило: иначе чужого человека можно править вслепую.

    Чужая строка для правки просто не находится (политика `using`), поэтому
    отказ выглядит как «изменено 0 строк», а не как ошибка базы. Это то же
    поведение, что у остальных таблиц с точкой.
    """
    alien = people.execute(
        "select id from employees where external_id = 'bg1'"
    ).fetchone()[0]
    with as_app_user(people, USER_MANAGER) as conn:
        conn.execute("savepoint attempt")
        changed = conn.execute(
            "update employees set last_name = 'ПОДМЕНА' where id = %s", (alien,)
        ).rowcount
        conn.execute("rollback to savepoint attempt")
    assert changed == 0

    # А своего — правит: ограничение не должно превращаться в «нельзя ничего».
    mine = people.execute(
        "select id from employees where external_id = 'ns1'"
    ).fetchone()[0]
    with as_app_user(people, USER_MANAGER) as conn:
        assert conn.execute(
            "update employees set last_name = 'ns1' where id = %s", (mine,)
        ).rowcount == 1


def test_without_context_nothing_is_visible(people):
    with as_app_user(people, None) as conn:
        assert conn.execute("select count(*) from employees").fetchone()[0] == 0


# --- доказательство, что режет политика, а не случайность --------------------


def test_the_protection_is_the_policy_and_not_luck(people):
    """Снимаем политику внутри транзакции теста — проверка обязана покраснеть.

    Тест, зелёный и до, и после починки, не доказывает ничего. Здесь порча
    делается прямо в прогоне: без `unit_visibility` на `employees` управляющий
    снова видит всю сеть.
    """
    people.execute("savepoint before_damage")
    people.execute("drop policy unit_visibility on employees")
    try:
        with as_app_user(people, USER_MANAGER) as conn:
            assert names(conn) == [
                "bg1", "fired-ns1", "moved", "no-terms", "ns1",
            ], "без политики управляющий обязан видеть всех — иначе тест ничего не проверяет"
    finally:
        people.execute("rollback to savepoint before_damage")

    # И снова закрыто — порча не пережила теста.
    with as_app_user(people, USER_MANAGER) as conn:
        assert names(conn) == ["fired-ns1", "moved", "ns1"]


def test_the_period_boundary_is_not_a_visibility_rule(people):
    """Версия из будущего тоже делает человека своим.

    Проверка того, что решение принято, а не выведено из дат: условия найма,
    начинающиеся позже, — это принятый на работу человек, а не чужой.
    """
    future = make_employee(people, "future")
    add_term(people, future, U_NS1, since=JULY)
    with as_app_user(people, USER_MANAGER) as conn:
        assert conn.execute(
            "select count(*) from employees where external_id = 'future'"
        ).fetchone()[0] == 1
