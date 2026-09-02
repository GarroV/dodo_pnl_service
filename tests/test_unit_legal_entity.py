"""Точка переезжает в другое юрлицо — новой версией с даты (T189, issue #179).

Эталон (модуль 11) говорит это прямо: «Точка меняет юрлицо так же, как сотрудник
меняет ставку: новой версией с даты. Прошлые месяцы остаются за старым юрлицом,
иначе разъедется отчётность обоих».

До этой задачи связь была одна на всю жизнь точки: перенос делался правкой поля,
то есть задним числом и без следа — закрытые месяцы молча переезжали в другое
юрлицо вместе с точкой. Это тот же класс, что D020 запрещает для расчёта:
закрытый период не меняется тихо.

Проверки идут ролью `app_user` там, где речь о доступе, и владельцем схемы там,
где речь о самой схеме: непересечение периодов держит `EXCLUDE`, а он на всех
одинаков.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    LE1,
    LE2,
    T1,
    U_BG1,
    USER_ADMIN,
    USER_MANAGER,
    as_app_user,
)


def link(conn, *, unit=U_BG1, entity=LE1, since="2023-01-01", until=None, tenant=T1):
    return conn.execute(
        """insert into unit_legal_entities (tenant_id, unit_id, legal_entity_id, valid_from, valid_to)
           values (%s, %s, %s, %s, %s) returning id""",
        (tenant, unit, entity, since, until),
    ).fetchone()[0]


def at(conn, day: str, *, unit=U_BG1):
    return conn.execute("select unit_legal_entity_at(%s, %s)", (unit, day)).fetchone()[0]


# ------------------------------------------------------------------ версии

def test_the_link_of_a_unit_is_read_by_the_date_asked_about(db):
    """Один и тот же вопрос про май и про июль даёт разные юрлица — в этом всё."""
    db.execute("delete from unit_legal_entities where unit_id = %s", (U_BG1,))
    link(db, entity=LE1, since="2023-01-01", until="2026-07-01")
    link(db, entity=LE2, since="2026-07-01")

    assert str(at(db, "2026-05-01")) == LE1
    assert str(at(db, "2026-06-30")) == LE1, "конец периода не входит — стык, а не пересечение"
    assert str(at(db, "2026-07-01")) == LE2


def test_two_legal_entities_cannot_own_a_unit_on_the_same_day(db):
    """Точка в двух юрлицах одновременно — это расход, посчитанный дважды."""
    db.execute("delete from unit_legal_entities where unit_id = %s", (U_BG1,))
    link(db, entity=LE1, since="2023-01-01")
    with pytest.raises(psycopg.errors.ExclusionViolation), db.transaction():
        link(db, entity=LE2, since="2026-07-01")


def test_a_unit_may_have_no_legal_entity_at_all_on_a_date(db):
    """Точка, заведённая до юрлица, — обычное дело, и это не ошибка."""
    db.execute("delete from unit_legal_entities where unit_id = %s", (U_BG1,))
    link(db, entity=LE1, since="2026-07-01")
    assert at(db, "2026-05-01") is None


def test_the_dates_of_a_version_go_in_order(db):
    """Версия, которая кончается раньше, чем началась, — это опечатка, не правило."""
    db.execute("delete from unit_legal_entities where unit_id = %s", (U_BG1,))
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        link(db, since="2026-07-01", until="2026-06-01")


# ------------------------------------------------------------------- доступ

def test_only_the_one_who_keeps_the_directories_moves_a_unit(db):
    """Перенос точки — это правка справочника, и правом закрыт как справочник."""
    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            link(conn, entity=LE2, since="2026-07-01")


def test_the_one_who_keeps_the_directories_moves_a_unit(db):
    """Иначе перенести точку было бы некому, и задача бессмысленна."""
    db.execute("delete from unit_legal_entities where unit_id = %s", (U_BG1,))
    db.execute(
        """insert into unit_legal_entities (tenant_id, unit_id, legal_entity_id, valid_from, valid_to)
           values (%s, %s, %s, '2023-01-01', '2026-07-01')""",
        (T1, U_BG1, LE1),
    )
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        link(conn, entity=LE2, since="2026-07-01")
        assert str(at(conn, "2026-07-01")) == LE2


def test_the_history_of_another_partner_is_invisible(db):
    """Кто чьё юрлицо — сведения партнёра, и изоляция здесь такая же, как везде."""
    from conftest import USER_OTHER

    with as_app_user(db, USER_OTHER) as conn:
        seen = conn.execute(
            "select count(*) from unit_legal_entities where unit_id = %s", (U_BG1,)
        ).fetchone()[0]
    assert seen == 0


# ---------------------------------------------------- снимок и история в ногу

def test_the_column_follows_the_versions(db):
    """`units.legal_entity_id` — снимок «сейчас», и держит его база, а не память.

    Правку истории делают из разных мест; требовать от каждого обновить ещё и
    колонку значит однажды получить точку, у которой колонка говорит одно, а
    история другое, — и молча.
    """
    db.execute("delete from unit_legal_entities where unit_id = %s", (U_BG1,))
    link(db, entity=LE2, since="2023-01-01")
    got = db.execute("select legal_entity_id from units where id = %s", (U_BG1,)).fetchone()[0]
    assert str(got) == LE2


def test_a_future_move_does_not_touch_todays_column(db):
    """Перенос с завтрашнего дня — это про завтра, и сегодня ничего не меняет."""
    db.execute("delete from unit_legal_entities where unit_id = %s", (U_BG1,))
    link(db, entity=LE1, since="2023-01-01", until="2099-01-01")
    link(db, entity=LE2, since="2099-01-01")
    got = db.execute("select legal_entity_id from units where id = %s", (U_BG1,)).fetchone()[0]
    assert str(got) == LE1


def test_editing_the_column_writes_a_version(db):
    """Пять мест заводят и правят точки. Историю за них ведёт база.

    Иначе точка, заведённая сидом или платформенной админкой, осталась бы без
    единой версии — при заполненной колонке, то есть незаметно.
    """
    db.execute("update units set legal_entity_id = %s where id = %s", (LE2, U_BG1))
    rows = db.execute(
        """select legal_entity_id, valid_from, valid_to
             from unit_legal_entities where unit_id = %s order by valid_from""",
        (U_BG1,),
    ).fetchall()
    assert [str(row[0]) for row in rows] == [LE1, LE2]
    assert rows[0][2] == rows[1][1], "старая версия не закрыта тем днём, с которого идёт новая"


def test_the_first_version_starts_when_the_unit_opened(db):
    """Точка, заведённая сегодня, но открытая год назад, не была ничьей весь год."""
    fresh = db.execute(
        """insert into units (tenant_id, legal_entity_id, code, title, opened_at)
           values (%s, %s, 'NEW1', 'Свежая', '2024-03-01') returning id""",
        (T1, LE1),
    ).fetchone()[0]
    assert str(at(db, "2024-06-01", unit=fresh)) == LE1
