"""Администратор платформы ведёт пространства — и только их (D065, issue #221).

Владельцу нужна платформенная админка: список пространств, внутрь пространства —
его сотрудники, выдача ролей. Чтобы это стало возможным, миграция `0261` открыла
роли приложения четыре таблицы: `tenants`, `users`, `memberships` и справочник
`roles`.

Открытая дверь в стене, на которой стоит изоляция партнёров, стоит того, чтобы
проверить её с обеих сторон. Здесь проверяется трижды:

1. **Дверь открывается тому, кому положено** — иначе админку не построить.
2. **Не открывается больше никому.** Администратор партнёра — даже самый полный,
   с `roles.manage` и всеми регистрами — по-прежнему не видит соседа и не может
   завести ни пространство, ни учётку.
3. **Дверь узкая.** Платформенное право даёт пространства, людей и роли — и НЕ
   даёт зарплат, табелей и фактов партнёров. Это главная проверка файла: она
   доказывает, что политики остальных таблиц действительно не тронуты, а не что
   их «вроде бы не меняли».

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в тестовой базе
суперпользователь: политики его не ограничивают, `force row level security` он
обходит. Запрет, проверенный без переключения роли, зелен всегда — на этом
проекте так уже прожил незамеченным дефект видимости регистров.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from conftest import (
    JUNE,
    T2,
    USER_ACCOUNTANT,
    USER_ADMIN,
    USER_DIRECTOR,
    as_app_user,
)

pytestmark = pytest.mark.usefixtures("db")

DENIED = psycopg.errors.InsufficientPrivilege

# Роли партнёра, включая самую полную. Ни одна из них платформенной не является:
# право платформы намеренно лежит вне ролей (T165).
PARTNER_ROLES = [USER_ADMIN, USER_DIRECTOR, USER_ACCOUNTANT]


@pytest.fixture
def platform_admin(db):
    """Сделать администратора сети ещё и администратором платформы.

    Строка кладётся владельцем схемы: из продукта в `platform_admins` не пишет
    ничего, и это условие, на котором дверь держится (`0248`, миграция `0261`).
    """
    db.execute(
        "insert into platform_admins (user_id, note) values (%s, 'тест') "
        "on conflict (user_id) do nothing",
        (USER_ADMIN,),
    )
    yield USER_ADMIN
    db.execute("delete from platform_admins where user_id = %s", (USER_ADMIN,))


class _Rollback(Exception):
    """Выйти из вложенной транзакции, не оставив в базе тестовых строк."""


# --- 1. дверь открывается тому, кому положено --------------------------------


def test_platform_admin_sees_every_space(db, platform_admin):
    """Список пространств — первый экран админки, и без этого его нет."""
    with as_app_user(db, platform_admin) as conn:
        seen = conn.execute("select count(*) from tenants").fetchone()[0]
    assert seen >= 2, "администратор платформы должен видеть все пространства, а не своё"


def test_platform_admin_creates_a_space(db, platform_admin):
    """Кнопка «Создать пространство» — то, ради чего заводилась миграция."""
    with as_app_user(db, platform_admin) as conn, pytest.raises(_Rollback):
        with conn.transaction():
            conn.execute(
                "insert into tenants (id, code, title, country_code, base_currency, report_currency) "
                "values (%s, 'new-partner', 'Новый партнёр', 'RS', 'RSD', 'EUR')",
                (str(uuid.uuid4()),),
            )
            raise _Rollback()


def test_platform_admin_creates_a_user_and_gives_a_role(db, platform_admin):
    """Первый человек пространства и его роль заводятся одним движением."""
    person = str(uuid.uuid4())
    with as_app_user(db, platform_admin) as conn, pytest.raises(_Rollback):
        with conn.transaction():
            conn.execute(
                "insert into users (id, username, password, full_name) "
                "values (%s, %s, 'x', 'Первый человек партнёра')",
                (person, f"person-{person[:8]}"),
            )
            # Роль берётся из справочника чужого пространства — значит его тоже
            # надо видеть, иначе выдавать было бы нечего.
            role_id = conn.execute(
                "select id from roles where tenant_id = %s limit 1", (T2,)
            ).fetchone()
            assert role_id, "справочник ролей чужого пространства не виден"
            conn.execute(
                "insert into memberships (tenant_id, user_id, role_id) values (%s, %s, %s)",
                (T2, person, role_id[0]),
            )
            raise _Rollback()


# --- 2. и не открывается больше никому ---------------------------------------


@pytest.mark.parametrize("user_id", PARTNER_ROLES)
def test_partner_roles_see_only_their_own_space(db, user_id):
    """Самая полная роль партнёра соседа по-прежнему не видит."""
    with as_app_user(db, user_id) as conn:
        seen = conn.execute(
            "select count(*) from tenants where id = %s", (T2,)
        ).fetchone()[0]
    assert seen == 0, "администратор партнёра видит чужое пространство"


@pytest.mark.parametrize("user_id", PARTNER_ROLES)
def test_partner_roles_cannot_create_a_space(db, user_id):
    """Пространство заводит платформа, а не партнёр (D064)."""
    with as_app_user(db, user_id) as conn:
        with pytest.raises(DENIED):
            with conn.transaction():
                conn.execute(
                    "insert into tenants (id, code, title, country_code, base_currency, report_currency) "
                    "values (%s, 'sneaky', 'Сам себе партнёр', 'RS', 'RSD', 'EUR')",
                    (str(uuid.uuid4()),),
                )


@pytest.mark.parametrize("user_id", PARTNER_ROLES)
def test_partner_roles_cannot_create_a_user(db, user_id):
    """Учётку заводит платформа. Иначе доступ в продукт раздаёт кто угодно."""
    with as_app_user(db, user_id) as conn:
        with pytest.raises(DENIED):
            with conn.transaction():
                conn.execute(
                    "insert into users (id, username, password) values (%s, %s, 'x')",
                    (str(uuid.uuid4()), f"sneaky-{uuid.uuid4().hex[:8]}"),
                )


def test_the_door_cannot_open_itself(db):
    """Право платформы нельзя выдать изнутри продукта — ни себе, ни другому.

    Без этого запрета вся конструкция бессмысленна: получив право писать в
    `memberships`, администратор партнёра выдал бы себе платформенное право и
    вышел за пределы своего пространства.
    """
    with as_app_user(db, USER_ADMIN) as conn:
        with pytest.raises(DENIED):
            with conn.transaction():
                conn.execute(
                    "insert into platform_admins (user_id, note) values (%s, 'сам себе')",
                    (USER_ADMIN,),
                )


# --- 3. дверь узкая: доступ, а не деньги -------------------------------------


@pytest.fixture
def money_of_another_partner(db):
    """Зарплатные данные чужого партнёра — материал, который НЕ должен быть виден.

    Заводятся владельцем схемы и убираются за собой. Без них проверка ниже
    считала бы нули в пустых таблицах и была бы зелёной при любых политиках —
    ровно та «проверка, которая проходит всегда», что хуже отсутствующей.
    Найдено при написании: первая версия теста именно так и проходила.
    """
    employee = db.execute(
        "insert into employees (tenant_id, external_id, first_name, last_name) "
        "values (%s, 'ext-platform-probe', 'Чужой', 'Сотрудник') returning id",
        (T2,),
    ).fetchone()[0]
    payrun = db.execute(
        "insert into payruns (tenant_id, period) values (%s, %s) returning id",
        (T2, JUNE),
    ).fetchone()[0]
    payslip = db.execute(
        "insert into payslips (tenant_id, payrun_id, employee_id) values (%s, %s, %s) returning id",
        (T2, payrun, employee),
    ).fetchone()[0]
    db.execute(
        "insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger) "
        "values (%s, %s, 'base', 'Оклад', 123456, 'official')",
        (T2, payslip),
    )
    db.execute(
        "insert into timesheets (tenant_id, employee_id, period, norm_hours, hours) "
        "values (%s, %s, %s, 176, '{\"regular\": 176}')",
        (T2, employee, JUNE),
    )
    yield
    db.execute("delete from timesheets where tenant_id = %s", (T2,))
    db.execute("delete from pay_components where tenant_id = %s", (T2,))
    db.execute("delete from payslips where tenant_id = %s", (T2,))
    db.execute("delete from payruns where tenant_id = %s", (T2,))
    db.execute("delete from employees where tenant_id = %s", (T2,))


@pytest.mark.parametrize(
    "table",
    ["payslips", "pay_components", "payruns", "timesheets", "employees"],
)
def test_platform_admin_sees_no_money_of_any_partner(
    db, platform_admin, money_of_another_partner, table
):
    """Платформенное право управляет доступом и не открывает финансы.

    Главная проверка файла. Соблазн сделать проще был: расширить
    `app_tenant_ids()`, чтобы она возвращала платформенному администратору все
    тенанты, — одна правка, и админка работает. Она же открыла бы ему весь
    продукт целиком, включая зарплаты живых людей у каждого партнёра.

    Проверяется перечислением таблиц, а не общим рассуждением: «политики
    остальных таблиц не трогали» — утверждение о намерении, а этот тест
    покраснеет, если однажды тронут. И проверяется на настоящих строках чужого
    партнёра, а не на пустой таблице (см. фикстуру выше).
    """
    assert db.execute(f"select count(*) from {table}").fetchone()[0] > 0, (
        f"в {table} нечего скрывать — проверка проверяла бы пустоту"
    )
    with as_app_user(db, platform_admin) as conn:
        seen = conn.execute(f"select count(*) from {table}").fetchone()[0]
    assert seen == 0, (
        f"платформенное право открыло {table}: администратор платформы видит "
        "данные партнёров, а должен видеть только пространства, людей и роли"
    )
