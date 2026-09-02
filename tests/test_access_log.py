"""История доступов: кто, кому, когда и зачем — и это не переписывается (T188).

Эталон подписывает таблицу прямо: «кто, кому, когда и зачем — не удаляется».
Слово «не удаляется» здесь — требование к базе, а не обещание интерфейса.
История доступов существует ровно для одного разговора: через полгода на
вопрос «почему у него были эти права в июне» отвечает запись, а не память. Если
запись можно поправить или стереть тем же экраном, который её создал, она этот
разговор не выдержит.

Поэтому запрет держится **правами таблицы**, а не политикой: у `app_user` нет
`update` и `delete` на `access_log` вовсе. Политику можно написать неверно и не
заметить; отсутствующее право отказывает всегда и одинаково.

Отдельно проверяется, что историю нельзя написать под чужим именем: запись
«Nikola выдал роль» ценна только тем, что её физически не мог сделать не Nikola.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    R_ADMIN,
    T1,
    USER_ACCOUNTANT,
    USER_ADMIN,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
)

WRITE = """insert into access_log
               (tenant_id, actor_user_id, subject_user_id, action, role_id,
                role_title, until, reason)
           values (%s, %s, %s, %s, %s, 'Администратор сети', null, %s)"""


def written(conn, *, actor=USER_ADMIN, subject=USER_MANAGER, tenant=T1,
            action="granted", reason="проверка"):
    return conn.execute(
        WRITE + " returning id", (tenant, actor, subject, action, R_ADMIN, reason),
    ).fetchone()[0]


def test_the_one_who_leads_roles_writes_the_history(db):
    """Иначе выдача роли просто не оставила бы следа."""
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        assert written(conn) is not None


def test_nobody_else_writes_the_history(db):
    """Бухгалтер не ведёт роли — значит и записей о выдачах не делает."""
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            written(conn, actor=USER_ACCOUNTANT)


def test_nobody_writes_the_history_under_another_name(db):
    """Запись «Х выдал роль» стоит ровно столько, сколько стоит имя в ней."""
    with as_app_user(db, USER_ADMIN) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            written(conn, actor=USER_MANAGER)


def test_the_history_cannot_be_rewritten(db):
    """Причина, названная в июне, обязана читаться в декабре той же."""
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        entry = written(conn)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "update access_log set reason = 'другая причина' where id = %s", (entry,)
            )


def test_the_history_cannot_be_deleted(db):
    """Самый простой способ переписать историю — стереть неудобную строку."""
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        entry = written(conn)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("delete from access_log where id = %s", (entry,))


def test_the_history_of_another_partner_is_invisible(db):
    """История доступов — это имена людей партнёра. Чужому её не видно."""
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        written(conn)
        with as_app_user(db, USER_OTHER) as other:
            seen = other.execute("select count(*) from access_log").fetchone()[0]
        assert seen == 0


def test_the_history_is_closed_to_those_who_do_not_lead_roles(db):
    """Кому не положено раздавать доступ, тому не положено и читать, кому его дали."""
    with as_app_user(db, USER_ADMIN) as conn, conn.transaction(force_rollback=True):
        written(conn)
        with as_app_user(db, USER_MANAGER) as manager:
            seen = manager.execute("select count(*) from access_log").fetchone()[0]
        assert seen == 0
