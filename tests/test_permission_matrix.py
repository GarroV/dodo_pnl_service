"""Матрица прав как конфигурация, а не спор о наборах (T203, issues #129, #130).

Q027 отвечен решением D060: выбирать одну картину доступа не нужно, наборы
обсуждаются с партнёром на месте, — значит строится **механизм настройки**.
Механизм состоит из двух частей, и проверяются здесь обе.

**Три состояния вместо двух.** У клетки «роль × право» не «есть/нет», а
`included` (входит в набор, ● эталона), `optional` (партнёр вправе выдать, ○) и
`never` (не выдаётся никогда, –). Третье состояние — не украшение: любой
конструктор прав из галочек рано или поздно соберёт опасную комбинацию, и
«никогда» делает её невозможной по построению, а не по бдительности того, кто
нажимает.

**Стена стоит в базе.** Ограничение на `roles.permissions` — `check`, а не
проверка представления: `check` действует на всех, включая владельца таблиц и
суперпользователя, то есть его нельзя обойти ни ролью, ни забытым `if` в новом
экране. Тем он и отличается от политик RLS, которые владелец обходит (и из-за
чего тесты доступа гоняются ролью `app_user`).
"""
from __future__ import annotations

import json

import psycopg
import pytest

from conftest import R_ADMIN, R_MANAGER, USER_ADMIN, as_app_user
from core.roles import (
    ALL_PERMISSIONS,
    INCLUDED,
    NEVER,
    OPTIONAL,
    ROLE_ORDER,
    ROLE_SHAPES,
    permission_states,
)

# ---------------------------------------------------------------- матрица кода

def test_the_matrix_names_a_state_for_every_permission_of_every_role():
    """Пустых клеток нет: «состояние не задано» читалось бы как «можно»."""
    for code in ROLE_ORDER:
        states = permission_states(code)
        assert set(states) == set(ALL_PERMISSIONS), code
        assert set(states.values()) <= {INCLUDED, OPTIONAL, NEVER}, code


def test_everything_the_role_ships_with_is_marked_included():
    """Набор роли и матрица — одно и то же, а не два списка рядом.

    Разъехавшись, они дали бы роль, которая приехала с правом, помеченным как
    «этой роли никогда»: база отвергла бы саму доставку роли.
    """
    for code in ROLE_ORDER:
        states = permission_states(code)
        for granted in ROLE_SHAPES[code].permissions:
            assert states[granted] == INCLUDED, f"{code}/{granted}"


def test_only_the_administrator_may_ever_manage_roles_and_rules():
    """Две стены эталона, названные там не значком, а словами.

    «Доступ выдаёт только администратор партнёра» и «менять правила расчёта —
    формулы начислений — только администратор». Остальные клетки, которых у
    роли нет, остаются `optional`: владелец 28.08.2026 просил гибкости прямо,
    и стена там, где он её не ставил, была бы решением за него.
    """
    for code in ROLE_ORDER:
        states = permission_states(code)
        if code == "admin":
            assert states["roles.manage"] == INCLUDED
            assert states["rules.manage"] == INCLUDED
        else:
            assert states["roles.manage"] == NEVER, code
            assert states["rules.manage"] == NEVER, code


def test_the_wall_is_the_exception_and_not_the_rule():
    """Стен ровно столько, сколько названо эталоном, — иначе это не гибкость."""
    walls = {
        (code, right)
        for code in ROLE_ORDER
        for right, state in permission_states(code).items()
        if state == NEVER
    }
    assert walls == {
        (code, right)
        for code in ("director", "accountant", "manager")
        for right in ("rules.manage", "roles.manage")
    }


# ------------------------------------------------------------------ стена базы

WALLED = """update roles
              set permission_states = '{"roles.manage": "never"}'::jsonb
            where id = %s"""


def test_the_database_refuses_a_permission_the_matrix_walls_off(db):
    """Администратор не выпишет управляющему право, которого тому не бывает."""
    db.execute(WALLED, (R_MANAGER,))
    with as_app_user(db, USER_ADMIN) as conn:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                """update roles
                      set permissions = permissions || '["roles.manage"]'::jsonb
                    where id = %s""",
                (R_MANAGER,),
            )


def test_the_wall_holds_for_the_owner_of_the_tables_too(db):
    """То, чего не даёт RLS: `check` не обходит никто, включая владельца схемы.

    Проверка нарочно идёт БЕЗ `as_app_user`: политики здесь ни при чём, и если
    бы стена держалась ими, эта проверка была бы зелёной, а стены бы не было.
    """
    db.execute(WALLED, (R_MANAGER,))
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            """update roles set permissions = '["roles.manage"]'::jsonb where id = %s""",
            (R_MANAGER,),
        )


def test_a_permission_marked_optional_is_granted_as_usual(db):
    """Стена — не запрет на настройку: ○ выдаётся тем же экраном и той же ролью."""
    db.execute(WALLED, (R_MANAGER,))
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        conn.execute(
            """update roles
                  set permissions = permissions || '["payrun.calculate"]'::jsonb
                where id = %s""",
            (R_MANAGER,),
        )
        got = conn.execute(
            "select permissions from roles where id = %s", (R_MANAGER,)
        ).fetchone()[0]
        assert "payrun.calculate" in got


def test_a_role_without_a_recorded_matrix_keeps_working(db):
    """Пустая матрица — «стен не заведено», а не «нельзя ничего».

    Так роли, заведённые до появления матрицы, продолжают жить: обратное
    означало бы, что миграция молча обесправила чужой стенд.
    """
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        conn.execute(
            """update roles
                  set permissions = permissions || '["roles.manage"]'::jsonb
                where id = %s""",
            (R_MANAGER,),
        )
        got = conn.execute(
            "select permissions from roles where id = %s", (R_MANAGER,)
        ).fetchone()[0]
        assert "roles.manage" in got


def test_the_matrix_of_the_administrator_walls_off_nothing(db):
    """Администратор может всё (D052) — значит у его роли нет ни одной стены."""
    db.execute(
        "update roles set permission_states = %s::jsonb where id = %s",
        (json.dumps(permission_states("admin")), R_ADMIN),
    )
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        conn.execute(
            "update roles set permissions = %s::jsonb where id = %s",
            (json.dumps(list(ALL_PERMISSIONS)), R_ADMIN),
        )
        got = conn.execute(
            "select permissions from roles where id = %s", (R_ADMIN,)
        ).fetchone()[0]
        assert len(got) == len(ALL_PERMISSIONS)
