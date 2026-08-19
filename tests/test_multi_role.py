"""Один человек — несколько ролей, права складываются (T170, D047).

**Откуда задача.** Владелец поправил разбор: людей не один и ролей не четыре
штуки на весь мир. Бухгалтер у части партнёров ведёт весь проект и тогда по
сути администратор; оперативный директор отвечает за сборку P&L целиком. Значит
правильный механизм — не «выдать администратору всё оптом», а дать одному
человеку несколько ролей сразу. Разделение обязанностей у тех партнёров, где
оно есть, от этого не ломается.

**Что здесь проверяется и почему именно так.**

Проверки идут **ролью `app_user`** (`as_app_user`): владелец таблиц политики
обходит, и «права складываются» можно написать неправильно с зелёным прогоном.
Именно так на этом проекте прожил незамеченным дефект видимости регистров.

Складывать обязана база, а не форма. Поэтому спрашиваются сами функции
разграничения — `app_has_permission`, `app_visible_ledgers`,
`app_sees_every_ledger`, `app_unit_ids` — и, что важнее, проверяется
**запись**: право, которое видно функцией, но не пропускает строку, ничего не
стоит.

Отдельно проверяется `Principal`: в базе права уже складывались (все четыре
функции написаны множеством), а приложение брало `Membership…first()` — то есть
человеку с двумя ролями продукт показывал одну. Красный тест на это и был
единственным, чего не хватало.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    R_ADMIN,
    R_DIRECTOR,
    T1,
    U_NS1,
    USER_ACCOUNTANT,
    USER_MANAGER,
    as_app_user,
    body,
    login_as,
)

# Роли фикстуры, из которых собираются пары. Бухгалтер + администратор — ровно
# тот случай, который описан в D047 словами владельца.
SECOND_ROLE = {"accountant": R_ADMIN, "manager": R_DIRECTOR}


def give_second_role(conn, user_id: str, role_id: str, unit_ids=None) -> None:
    """Выдать человеку вторую роль — так, как это сделает экран ролей.

    Владельцем схемы, а не ролью приложения: это подготовка материала, а не
    проверяемое действие. Само право менять членства проверяется отдельно
    (`test_roles_screen.py`).
    """
    conn.execute(
        "insert into memberships (tenant_id, user_id, role_id, unit_ids)"
        " values (%s, %s, %s, %s)",
        (T1, user_id, role_id, unit_ids),
    )


# --- база: права складываются ------------------------------------------------


def test_permissions_add_up_across_roles(db):
    """Бухгалтер + администратор = права обоих, а не одной из ролей."""
    give_second_role(db, USER_ACCOUNTANT, R_ADMIN)

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        # Своё, из роли бухгалтера.
        assert conn.execute(
            "select app_has_permission(%s, 'payrun.calculate')", (T1,)
        ).fetchone()[0] is True
        # Чужое, из роли администратора.
        assert conn.execute(
            "select app_has_permission(%s, 'directory.manage')", (T1,)
        ).fetchone()[0] is True
        # Того, чего нет ни у одной из двух ролей, не появляется.
        assert conn.execute(
            "select app_has_permission(%s, 'выдуманное.право')", (T1,)
        ).fetchone()[0] is False


def test_the_accountant_with_the_admin_role_keeps_writing_all_three_ledgers(db):
    """Вторая роль не сужает первую: строки всех трёх регистров по-прежнему пишутся.

    Это половина условия готовности T170. Вторая половина — справочники, ниже.
    Проверяется записью, а не функцией: политики регистров стоят на записи, и
    «функция вернула массив» про них ничего не говорит.
    """
    from conftest import pay_component

    give_second_role(db, USER_ACCOUNTANT, R_ADMIN)

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        for ledger in ("official", "supplementary", "internal"):
            pay_component(conn, ledger=ledger, code=f"multi.{ledger}")
        written = conn.execute(
            "select count(distinct ledger) from pay_components where code like 'multi.%'"
        ).fetchone()[0]
    assert written == 3, "роль с двумя членствами перестала писать все регистры"


def test_the_accountant_with_the_admin_role_keeps_the_directory(db):
    """Вторая половина условия готовности: справочники ему теперь открыты.

    До T170 бухгалтер, ведущий у партнёра весь проект, не мог завести человека:
    `directory.manage` есть только у администратора, а выписать себе роль было
    нечем — экрана ролей не существовало (issue #77).
    """
    give_second_role(db, USER_ACCOUNTANT, R_ADMIN)

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        conn.execute(
            "insert into employees (tenant_id, external_id, first_name, last_name)"
            " values (%s, 'multi-role-1', 'Новый', 'Сотрудник')",
            (T1,),
        )
        assert conn.execute(
            "select count(*) from employees where external_id = 'multi-role-1'"
        ).fetchone()[0] == 1


def test_one_role_alone_still_refuses(db):
    """Порча наоборот: без второй роли справочник по-прежнему закрыт.

    Без этой проверки предыдущая была бы зелёной и в мире, где `directory.manage`
    не проверяется вовсе.
    """
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        # Точка сохранения обязательна: отвергнутый оператор роняет транзакцию
        # целиком, и уборка `as_app_user` (`reset role`) упала бы следом —
        # тест краснел бы не тем, что проверяет.
        with pytest.raises(psycopg.errors.Error), conn.transaction():
            conn.execute(
                "insert into employees (tenant_id, external_id, first_name, last_name)"
                " values (%s, 'multi-role-2', 'Никакой', 'Сотрудник')",
                (T1,),
            )


def test_visible_ledgers_add_up(db):
    """Регистры — объединение наборов, а не набор одной из ролей.

    Пара выбрана та, где сложение видно: у управляющего два регистра (D031), у
    директора три. На бухгалтере это не проверить — у него набор уже полон
    (D036), и отличить объединение от «взяли первую роль» было бы нельзя.
    """
    give_second_role(db, USER_MANAGER, R_DIRECTOR)

    with as_app_user(db, USER_MANAGER) as conn:
        ledgers = conn.execute("select app_visible_ledgers(%s)", (T1,)).fetchone()[0]
        every = conn.execute("select app_sees_every_ledger(%s)", (T1,)).fetchone()[0]

    assert set(ledgers) == {"official", "supplementary", "internal"}
    assert every is True, "итоги расчёта остались бы скрыты от того, кому видны все регистры"


def test_units_add_up_to_all_units(db):
    """Точки: членство без списка точек открывает все, а не пересекается со своей.

    У управляющего в фикстуре одна точка, у директора — ни одной записанной, то
    есть все. Пересечение дало бы одну точку и было бы тем самым «запрещать
    меньшее тому, кому разрешено большее» (D033).
    """
    give_second_role(db, USER_MANAGER, R_DIRECTOR)

    with as_app_user(db, USER_MANAGER) as conn:
        units = conn.execute("select app_unit_ids(%s)", (T1,)).fetchone()[0]
        visible = conn.execute(
            "select count(*) from units where tenant_id = %s", (T1,)
        ).fetchone()[0]

    assert units is None, "null означает «все точки тенанта» — объединение дало не его"
    assert visible == 3, "по точкам человека по-прежнему режет одна роль из двух"


def test_a_single_unit_role_still_sees_one_unit(db):
    """Порча наоборот: без второй роли управляющий видит ровно свою точку."""
    with as_app_user(db, USER_MANAGER) as conn:
        units = conn.execute("select app_unit_ids(%s)", (T1,)).fetchone()[0]
        visible = conn.execute(
            "select count(*) from units where tenant_id = %s", (T1,)
        ).fetchone()[0]

    assert [str(u) for u in units] == [U_NS1]
    assert visible == 1


# --- приложение: продукт показывает обе роли ----------------------------------


@pytest.fixture
def accountant_is_also_admin(web_env):
    """Бухгалтеру сида выдана вторая роль администратора — и убрана после теста.

    Владельцем схемы: членства — общее состояние базы веб-тестов, и оставленное
    лишнее членство молча поменяло бы права соседним модулям.
    """
    with psycopg.connect(web_env, autocommit=True) as conn:
        conn.execute(
            "insert into memberships (tenant_id, user_id, role_id)"
            " select m.tenant_id, m.user_id, r.id"
            "   from memberships m"
            "   join users u on u.id = m.user_id"
            "   join roles r on r.tenant_id = m.tenant_id and r.code = 'admin'"
            "  where u.username = 'accountant'"
            "  on conflict do nothing"
        )
    yield web_env
    with psycopg.connect(web_env, autocommit=True) as conn:
        conn.execute(
            "delete from memberships m using users u, roles r"
            " where m.user_id = u.id and m.role_id = r.id"
            "   and u.username = 'accountant' and r.code = 'admin'"
        )


def test_the_product_adds_up_the_rights_of_both_roles(client, accountant_is_also_admin):
    """Бухгалтер со второй ролью ведёт справочники и по-прежнему считает период.

    Страницей, а не полем `Principal`: проверяется то, что увидит человек.
    """
    login_as(client, "accountant")

    directory = client.get("/directory/")
    assert directory.status_code == 200
    assert "не входит в права вашей роли" not in body(directory)

    # Первая роль на месте: расчёт периода никуда не делся.
    from conftest import period_url

    page = body(client.get(period_url(client)))
    assert "Расчёт периода не входит в права" not in page


def test_the_header_names_both_roles(client, accountant_is_also_admin):
    """В шапке видно обе роли: человек должен понимать, чьими глазами смотрит.

    Одно название вместо двух — это не косметика: отказ по правам называет роль
    («… не входит в права вашей роли «Бухгалтер»»), и с одной ролью из двух он
    указывал бы не на то.
    """
    login_as(client, "accountant")

    page = body(client.get("/periods/"))

    assert "Бухгалтер" in page
    assert "Администратор сети" in page


def test_without_the_second_role_the_directory_is_refused(client, web_env):
    """Порча наоборот: одна роль — и справочники по-прежнему закрыты."""
    login_as(client, "accountant")

    response = client.get("/directory/")

    assert response.status_code == 403
    assert "не входит в права вашей роли" in body(response)
