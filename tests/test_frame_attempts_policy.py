"""Журнал попыток выйти за рамку держит база, а не экран (T192).

**Зачем отдельно от экранного теста.** Там проверяется продукт: отказ словами,
запись попытки, строка на карточке правила. Здесь — что журнал остаётся
журналом, если писать в него мимо экрана: через будущий API, через Telegram,
через чужой скрипт с теми же доступами к базе. Половина пары, проверенная за
обе, — ровно тот способ, которым в этом проекте уже прожил незамеченным дефект
видимости регистров.

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в тестовой базе
суперпользователь: политики его не ограничивают, `force row level security` он
обходит. Запрет, проверенный без переключения роли, зелен всегда.

**Что именно защищается.** След аудита. Отказ, который можно вычеркнуть, ничем
не лучше отказа, который никуда не записан: и то и другое читается как «никто
не пробовал». Поэтому `update` и `delete` не разрешены никому — ни партнёру, ни
администратору сети.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    T1,
    T2,
    USER_ADMIN,
    USER_DIRECTOR,
    USER_OTHER,
    as_app_user,
)

pytestmark = pytest.mark.usefixtures("db")

DENIED = psycopg.errors.InsufficientPrivilege

# Кто есть кто в фикстуре, поимённо — а не «первый держатель роли»:
# `USER_ADMIN` — администратор сети партнёра, единственный с `rules.manage`;
# `USER_DIRECTOR` — директор, ведёт месяц и правила НЕ ведёт;
# `USER_OTHER` — человек второго партнёра, `T2`.
FRAME = "'{\"mode\": \"frame\", \"min\": \"country\", \"source\": \"чл. 108\"}'::jsonb"

INSERT = f"""
    insert into rule_frame_attempts
        (tenant_id, path, scope_type, wanted, country_value, frame, valid_from,
         created_by_name)
    values (%s, 'hour_types.night.pay_percent', 'tenant', '1.1'::jsonb,
            '1.26'::jsonb, {FRAME}, date '2026-09-01', 'проверка')
"""


def _attempt(conn, tenant: str = T1) -> str:
    """Строка журнала, положенная владельцем схемы.

    Здесь это подготовка, а не обход: проверяется, что с ней смогут сделать
    роли, а не то, как она туда попала.
    """
    return conn.execute(INSERT + " returning id", (tenant,)).fetchone()[0]


# --- пишет только тот, кто ведёт правила ---------------------------------------


def test_the_director_cannot_write_to_the_journal(db):
    """Роль без `rules.manage` в журнал не пишет.

    Директор ведёт месяц и утверждает расчёт, но правила не правит — значит и
    попыток от него быть не может. Строка, положенная им, была бы записью о
    событии, которого не происходило.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(INSERT, (T1,))
        conn.execute("rollback to savepoint attempt")


def test_the_admin_writes_and_reads_the_journal(db):
    """Администратор сети — тот, кто правила ведёт, — пишет и видит свою строку."""
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute(INSERT, (T1,))
        assert conn.execute(
            "select count(*) from rule_frame_attempts where tenant_id = %s", (T1,)
        ).fetchone()[0] == 1


def test_the_director_does_not_read_the_journal(db):
    """И не читает: журнал лежит на карточке правила, которой у него нет.

    Право вести правила и право видеть отказы по ним — одно право: разводить их
    значило бы утверждать, что кому-то нужна одна половина без другой.
    """
    _attempt(db)
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select count(*) from rule_frame_attempts").fetchone()[0] == 0


# --- чужого не видно ------------------------------------------------------------


def test_a_stranger_does_not_see_another_partners_attempts(db):
    """Попытки соседнего партнёра не видны — это первое, что держит изоляция."""
    _attempt(db, T1)
    with as_app_user(db, USER_OTHER) as conn:
        assert conn.execute("select count(*) from rule_frame_attempts").fetchone()[0] == 0


def test_nothing_is_visible_without_a_user_context(db):
    """Без выставленного контекста журнал пуст, а не «весь».

    Забытый `set_config('app.user_id')` обязан означать «ничего», иначе
    соединение без входа читало бы всё подряд.
    """
    _attempt(db)
    with as_app_user(db, None) as conn:
        assert conn.execute("select count(*) from rule_frame_attempts").fetchone()[0] == 0


def test_an_attempt_cannot_be_planted_for_another_partner(db):
    """Свою попытку в чужой журнал не положить — проверка на записи, не только на чтении."""
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(psycopg.errors.Error):
            conn.execute(INSERT, (T2,))
        conn.execute("rollback to savepoint attempt")


# --- журнал не переписывается ---------------------------------------------------


def test_nobody_edits_an_attempt(db):
    """Правки нет ни у кого, включая того, кто эту строку и написал.

    Отказ громкий (`InsufficientPrivilege`), а не тихое «изменено 0 строк»: право
    на `update` отозвано у роли, и добавленная кем-нибудь политика этого не
    отменит — два замка, как у `platform_admins`.
    """
    _attempt(db)
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute("update rule_frame_attempts set wanted = '9'::jsonb")
        conn.execute("rollback to savepoint attempt")


def test_nobody_deletes_an_attempt(db):
    """И удаления нет: журнал, из которого можно вычеркнуть строку, не журнал."""
    _attempt(db)
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute("delete from rule_frame_attempts")
        conn.execute("rollback to savepoint attempt")
