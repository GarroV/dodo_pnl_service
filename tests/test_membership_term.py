"""Роль на срок: доступ, который сам заканчивается (T188, issue #178).

Эталон рисует живой сценарий: «12.06.2026 — Nikola Ilić выдал Milica Jovanović
роль администратора до 31.07.2026. Причина: отпуск партнёра, нужно закрыть
июнь», и рядом закон модели: «в этот день роль сама вернётся к прежней».

Ценность срока целиком в том, что база перестаёт его учитывать сама. Срок,
который знает только экран, — это не срок, а надпись: доступ остаётся, а на
вопрос «кончился ли он» отвечает тот, кто вовремя посмотрел.

Поэтому проверяется **каждая** функция контекста по отдельности. Их шесть, и
все они читают `memberships` своим запросом: забыть срок в одной из них —
значит оставить просроченной роли ровно ту дверь, которую эта функция открывает.
Все проверки идут ролью `app_user`: владелец таблиц политики обходит.
"""
from __future__ import annotations

from conftest import (
    R_ADMIN,
    R_OTHER,
    T1,
    T2,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
)

YESTERDAY = "current_date - 1"
TODAY = "current_date"


def grant(conn, *, user=USER_MANAGER, role=R_ADMIN, tenant=T1, until=YESTERDAY):
    """Выдать человеку вторую роль со сроком — материал для проверок ниже."""
    conn.execute(
        f"""insert into memberships (tenant_id, user_id, role_id, expires_at)
            values (%s, %s, %s, {until})""",
        (tenant, user, role),
    )


def test_an_expired_role_gives_no_permission(db):
    """Вчерашний срок — и права нет уже сегодня, а не «когда кто-нибудь уберёт»."""
    grant(db)
    with as_app_user(db, USER_MANAGER) as conn:
        got = conn.execute(
            "select app_has_permission(%s, 'roles.manage')", (T1,)
        ).fetchone()[0]
    assert got is False


def test_a_role_that_runs_out_today_still_works_today(db):
    """«До 31.07» — значит 31 июля роль ещё действует.

    Граница включающая, потому что так её читает человек: «выдал до 31.07»
    никто не понимает как «кончится 30-го».
    """
    grant(db, until=TODAY)
    with as_app_user(db, USER_MANAGER) as conn:
        got = conn.execute(
            "select app_has_permission(%s, 'roles.manage')", (T1,)
        ).fetchone()[0]
    assert got is True


def test_an_expired_role_gives_no_ledgers_of_its_own(db):
    """Роль администратора открывала внутренний регистр — с концом срока закрыла."""
    grant(db)
    with as_app_user(db, USER_MANAGER) as conn:
        got = conn.execute("select app_visible_ledgers(%s)", (T1,)).fetchone()[0]
    assert "internal" not in got


def test_an_expired_role_does_not_widen_the_units(db):
    """Членство без списка точек означает «все точки» — но не просроченное.

    Самая тихая из возможных утечек: `app_unit_ids` вернула бы `null`, то есть
    «ограничения нет», и управляющий одной точки увидел бы все — при том что
    роль, которая это дала, кончилась.
    """
    grant(db)
    with as_app_user(db, USER_MANAGER) as conn:
        got = conn.execute("select app_unit_ids(%s)", (T1,)).fetchone()[0]
    assert got is not None, "просроченная роль открыла все точки"
    assert len(got) == 1


def test_an_expired_membership_does_not_open_another_partner(db):
    """Изоляция партнёров стоит на `app_tenant_ids` — и срок обязан её сужать."""
    grant(db, role=R_OTHER, tenant=T2)
    with as_app_user(db, USER_MANAGER) as conn:
        got = conn.execute("select array(select app_tenant_ids())").fetchone()[0]
    assert T2 not in [str(value) for value in got]


def test_an_expired_role_no_longer_shows_a_colleague_by_name(db):
    """Имя коллеги видно по общему тенанту — общий тенант кончается вместе с ролью."""
    grant(db, user=USER_OTHER, role=R_ADMIN, tenant=T1)
    with as_app_user(db, USER_MANAGER) as conn:
        got = conn.execute(
            "select app_user_display_name(%s)", (USER_OTHER,)
        ).fetchone()[0]
    assert got is None


def test_an_expired_role_does_not_lead_the_country_calendar(db):
    """Производственный календарь страны — тоже дверь, и тоже через членство."""
    grant(db)
    with as_app_user(db, USER_MANAGER) as conn:
        got = conn.execute("select app_manages_calendar('RS')").fetchone()[0]
    assert got is False


def test_a_membership_without_a_date_lives_as_before(db):
    """Пустой срок — «навсегда». Так живёт всё, что заведено до появления срока."""
    with as_app_user(db, USER_MANAGER) as conn:
        got = conn.execute(
            "select app_has_permission(%s, 'timesheet.edit')", (T1,)
        ).fetchone()[0]
    assert got is True
