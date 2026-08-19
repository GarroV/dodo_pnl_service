"""Кто вправе менять роли и раздавать их людям (T171, issue #77).

Почему эти проверки вообще понадобились. До сих пор `roles.manage` было правом
без потребителя: экрана ролей нет, и `0130` честно написала, что права на роли
не проверяет — «заводить политику под ненаписанный экран это догадка». Экран
появляется, и вместе с ним всплывает то, чего никто не проверял:

**Сам себе роль.** Политика `memberships` из `0004` разрешает строку, где
`user_id = app_user_id()`, — она писалась как изоляция чтения («вижу свои
членства»), но она же разрешающая на запись. То есть управляющий точки мог
вписать себе членство с ролью администратора и получить все права, не спрашивая
никого. Это не теория: проверка ниже сначала была красной ровно так.

**Правка самой роли.** У `roles` не было ни одной политики записи, только
изоляция по тенанту, — а она `for all`. Значит любой сотрудник партнёра мог
дописать своей роли `payrun.calculate`.

Все проверки идут **ролью `app_user`**: владелец таблиц политики обходит, и
такая же проверка от его имени была бы зелёной при полностью дырявой базе.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    R_ACCOUNTANT,
    R_ADMIN,
    R_MANAGER,
    R_SYSTEM,
    T1,
    T2,
    USER_ACCOUNTANT,
    USER_ADMIN,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
)


def test_a_user_cannot_hand_themselves_another_role(db):
    """Управляющий не выписывает себе администратора.

    Самое дорогое из возможных повышений: одна строка в `memberships` — и
    человек получает и справочники, и правила, и роли.
    """
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "insert into memberships (tenant_id, user_id, role_id) values (%s, %s, %s)",
                (T1, USER_MANAGER, R_ADMIN),
            )


def test_a_user_cannot_add_rights_to_their_own_role(db):
    """Бухгалтер не дописывает своей роли право вести справочники."""
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                """update roles
                      set permissions = permissions || '["directory.manage"]'::jsonb
                    where id = %s""",
                (R_ACCOUNTANT,),
            )


def test_the_administrator_edits_the_rights_of_a_role(db):
    """У кого `roles.manage` — тот и правит. Иначе экран ролей бессмысленен."""
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        conn.execute(
            """update roles
                  set permissions = '["timesheet.edit", "unit.close", "payrun.calculate"]'::jsonb
                where id = %s""",
            (R_MANAGER,),
        )
        got = conn.execute(
            "select permissions from roles where id = %s", (R_MANAGER,)
        ).fetchone()[0]
        assert "payrun.calculate" in got


def test_the_common_role_is_editable_by_nobody(db):
    """Роль без тенанта — общая для всех партнёров, и правится не отсюда.

    Разрешить её правку значило бы дать пользователю одного партнёра менять
    то, что видят все остальные (тот же довод, что у общего справочника
    статей в `0004`).
    """
    with as_app_user(db, USER_ADMIN) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "update roles set permissions = '[\"roles.manage\"]'::jsonb where id = %s",
                (R_SYSTEM,),
            )


def test_the_administrator_gives_a_second_role_to_a_person(db):
    """Ради чего всё и затевалось (D047): бухгалтер становится ещё и админом."""
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        conn.execute(
            "insert into memberships (tenant_id, user_id, role_id) values (%s, %s, %s)",
            (T1, USER_ACCOUNTANT, R_ADMIN),
        )
        rows = conn.execute(
            "select count(*) from memberships where user_id = %s and tenant_id = %s",
            (USER_ACCOUNTANT, T1),
        ).fetchone()[0]
        assert rows == 2


def test_the_administrator_cannot_reach_into_another_partner(db):
    """Право `roles.manage` действует в своём тенанте, а не вообще."""
    with as_app_user(db, USER_ADMIN) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "insert into memberships (tenant_id, user_id, role_id) values (%s, %s, %s)",
                (T2, USER_OTHER, R_ADMIN),
            )


def test_only_the_role_manager_sees_who_works_at_the_partner(db):
    """Экран ролей показывает чужие членства — и только тому, кто их ведёт.

    Остальным по-прежнему видно только своё: кто ещё работает у партнёра, из
    ведомости не следует (`0004`, пункт про корневую таблицу).
    """
    with as_app_user(db, USER_ADMIN) as conn:
        seen = conn.execute(
            "select count(*) from memberships where tenant_id = %s", (T1,)
        ).fetchone()[0]
        assert seen == 4

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        seen = conn.execute(
            "select count(*) from memberships where tenant_id = %s", (T1,)
        ).fetchone()[0]
        assert seen == 1


def test_without_context_nothing_is_visible_and_nothing_is_writable(db):
    """Контекст не выставлен — пусто и отказ, а не «всё» (D014)."""
    with as_app_user(db, None) as conn:
        assert conn.execute("select count(*) from memberships").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "insert into memberships (tenant_id, user_id, role_id) values (%s, %s, %s)",
                (T1, USER_ADMIN, R_ADMIN),
            )


def test_the_role_manager_sees_people_by_name(db):
    """Экрану ролей нужны имена: столбик UUID не говорит, кому даёшь право.

    Учётки заводятся здесь же владельцем таблиц: схемная фикстура их не
    содержит — до экрана ролей ни один тест доступа людьми по именам не
    интересовался.
    """
    db.execute(
        """insert into users (id, username, password, full_name) values
               (%s, 'admin', 'x', 'Админ Сети'),
               (%s, 'accountant', 'x', 'Бухгалтер Партнёра'),
               (%s, 'manager', 'x', 'Управляющий NS1'),
               (%s, 'stranger', 'x', 'Чужой Партнёр')""",
        (USER_ADMIN, USER_ACCOUNTANT, USER_MANAGER, USER_OTHER),
    )

    with as_app_user(db, USER_ADMIN) as conn:
        names = {row[0] for row in conn.execute("select username from users").fetchall()}
    assert names == {"admin", "accountant", "manager"}, (
        f"администратор видит не своих или не всех: {names}"
    )

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        seen = {row[0] for row in conn.execute("select username from users").fetchall()}
    assert seen == {"accountant"}, "бухгалтеру по-прежнему видна только своя учётка"
